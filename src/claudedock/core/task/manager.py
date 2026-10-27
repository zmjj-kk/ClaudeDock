from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from claudedock.core.storage import atomic_write
from claudedock.core.task.model import Task, TaskStatus


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TaskManager:
    # 初始化：确保目录存在，扫描现有文件确定下一个 ID
    def __init__(self, tasks_dir: Path) -> None:
        self._dir = tasks_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._thread_lock = RLock()
        self._lock_depth = 0
        with self._locked():
            self._recover()
            self._next_id = self._max_id() + 1

    # 同一任务目录的多个管理器与进程共享文件锁，线程重入保持安全
    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            if self._lock_depth:
                yield
                return
            fd = os.open(
                self._dir / ".transaction.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600
            )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._lock_depth = 1
                try:
                    yield
                finally:
                    self._lock_depth = 0
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    # 从 task_<id>.json 文件名安全提取整数 ID；无法解析返回 None
    def _task_id(self, path: Path) -> int | None:
        parts = path.stem.split("_")
        if len(parts) != 2 or parts[0] != "task" or not parts[1].isdigit():
            return None
        return int(parts[1])

    # 返回按 ID 升序排列的合法 task 文件路径；跳过无法解析 ID 的异常文件
    def _task_files(self) -> list[Path]:
        return sorted(
            (f for f in self._dir.glob("task_*.json") if self._task_id(f) is not None),
            key=lambda p: self._task_id(p) or 0,
        )

    # 扫描目录中 task_*.json 文件，返回最大 ID（无文件则返回 0）
    def _max_id(self) -> int:
        return max((self._task_id(f) or 0 for f in self._task_files()), default=0)

    # 读取指定 ID 的任务文件
    def _load(self, task_id: int) -> Task:
        path = self._dir / f"task_{task_id}.json"
        if not path.exists():
            raise ValueError(f"task {task_id} not found")
        return Task.from_dict(json.loads(path.read_text()))

    # 将任务写入对应 JSON 文件
    def _save(self, task: Task) -> None:
        path = self._dir / f"task_{task.id}.json"
        atomic_write(path, json.dumps(task.to_dict(), indent=2, ensure_ascii=False))

    # 重放已经提交的任务批次日志，修复跨文件更新中断
    def _recover(self) -> None:
        journal = self._dir / "transaction.json"
        if journal.exists():
            for data in json.loads(journal.read_text(encoding="utf-8")):
                self._save(Task.from_dict(data))
            journal.unlink()

    # 写前日志提交整个任务批次，再生成兼容旧版本的文件投影
    def _commit_tasks(self, tasks: list[Task]) -> None:
        journal = self._dir / "transaction.json"
        atomic_write(journal, json.dumps([t.to_dict() for t in tasks], ensure_ascii=False))
        self._recover()

    # 验证全部依赖存在且图无环，失败时尚未修改任何文件
    def _validate_dag(self, tasks: list[Task]) -> None:
        graph = {t.id: t.blocked_by for t in tasks}
        visiting: set[int] = set()
        visited: set[int] = set()

        # 深度优先检测未知依赖与回边
        def visit(node: int) -> None:
            if node not in graph:
                raise ValueError(f"dependency task {node} not found")
            if node in visiting:
                raise ValueError("cyclic task dependency")
            if node in visited:
                return
            visiting.add(node)
            for dependency in graph[node]:
                visit(dependency)
            visiting.remove(node)
            visited.add(node)

        for node in graph:
            visit(node)

    # 创建时持有进程锁并重读最大 ID，防止并发管理器覆盖同一任务
    def create(
        self, subject: str, description: str = "", blocked_by: list[int] | None = None
    ) -> Task:
        with self._locked():
            self._recover()
            self._next_id = self._max_id() + 1
            return self._create(subject, description, blocked_by)

    # 创建新任务，写入 JSON 文件，返回 Task
    def _create(
        self,
        subject: str,
        description: str = "",
        blocked_by: list[int] | None = None,
    ) -> Task:
        for dep_id in blocked_by or []:
            if not (self._dir / f"task_{dep_id}.json").exists():
                raise ValueError(f"blocked_by task {dep_id} not found")
        now = _now()
        task = Task(
            id=self._next_id,
            subject=subject,
            description=description,
            status="pending",
            blocked_by=list(blocked_by or []),
            created_at=now,
            updated_at=now,
        )
        self._save(task)
        self._next_id += 1
        return task

    # 读取指定 ID 的任务
    def get(self, task_id: int) -> Task:
        with self._locked():
            self._recover()
            return self._load(task_id)

    # 将图校验与跨文件依赖清理放入同一进程锁
    def update(
        self,
        task_id: int,
        *,
        status: TaskStatus | None = None,
        add_blocked_by: list[int] | None = None,
        remove_blocked_by: list[int] | None = None,
    ) -> Task:
        with self._locked():
            return self._update(
                task_id,
                status=status,
                add_blocked_by=add_blocked_by,
                remove_blocked_by=remove_blocked_by,
            )

    # 更新任务状态或依赖列表；status="completed" 时自动清理其他任务的 blocked_by
    def _update(
        self,
        task_id: int,
        *,
        status: TaskStatus | None = None,
        add_blocked_by: list[int] | None = None,
        remove_blocked_by: list[int] | None = None,
    ) -> Task:
        self._recover()
        task = self._load(task_id)
        if status is not None:
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"invalid status: {status!r}")
            task.status = status
        if add_blocked_by:
            task.blocked_by = list(set(task.blocked_by + add_blocked_by))
        if remove_blocked_by:
            task.blocked_by = [x for x in task.blocked_by if x not in remove_blocked_by]
        task.updated_at = _now()
        tasks = [t for t in self.list_all() if t.id != task.id] + [task]
        self._validate_dag(tasks)
        if status == "completed":
            for dependent in tasks:
                dependent.blocked_by = [d for d in dependent.blocked_by if d != task_id]
        self._commit_tasks(tasks)
        return task

    # 在完整恢复事务之后返回任务快照，读者不会看到半个依赖更新
    def list_all(self) -> list[Task]:
        with self._locked():
            return self._list_all()

    # 返回所有任务，按 ID 升序排列
    def _list_all(self) -> list[Task]:
        self._recover()
        tasks = []
        for f in self._task_files():
            try:
                tasks.append(Task.from_dict(json.loads(f.read_text())))
            except (ValueError, KeyError):
                pass
        return tasks

    # 将 completed_id 从所有其他任务的 blocked_by 列表中移除
    def _clear_dependency(self, completed_id: int) -> None:
        for f in self._dir.glob("task_*.json"):
            try:
                data = json.loads(f.read_text())
                blocked = [int(x) for x in data.get("blocked_by") or []]
            except (ValueError, TypeError):
                continue
            if completed_id in blocked:
                data["blocked_by"] = [x for x in blocked if x != completed_id]
                data["updated_at"] = _now()
                atomic_write(f, json.dumps(data, indent=2, ensure_ascii=False))

    # 格式化任务列表摘要，供 task_list 工具返回给 Agent
    def format_list(self) -> str:
        tasks = self.list_all()
        if not tasks:
            return "No tasks."
        marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        lines = []
        for t in tasks:
            blocked = f" (blocked by: {t.blocked_by})" if t.blocked_by else ""
            lines.append(f"{marker.get(t.status, '[?]')} #{t.id}: {t.subject}{blocked}")
        return "\n".join(lines)
