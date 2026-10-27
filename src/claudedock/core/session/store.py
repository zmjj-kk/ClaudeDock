from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from claudedock.core.compact.projection import ProjectionStore
from claudedock.core.session.model import Session
from claudedock.core.storage import atomic_write

logger = logging.getLogger(__name__)

MessageContent = str | list[dict[str, Any]]


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class SessionStore:
    # 初始化 session 文件存储根目录
    def __init__(self, root: Path) -> None:
        self._root = root.expanduser()
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)

    # 返回指定 session 的目录路径
    def session_dir(self, sid: str) -> Path:
        if not sid or Path(sid).name != sid or sid in (".", "..") or "\\" in sid:
            raise ValueError("invalid session id")
        path = self._root / sid
        if path.is_symlink():
            raise ValueError("session directory cannot be a symlink")
        return path

    # 重放会话写前日志，保证用户消息与 Run 元数据一起恢复
    def recover(self, sid: str) -> None:
        directory = self.session_dir(sid)
        journal = directory / "session.transaction.json"
        if not journal.exists():
            return
        data = json.loads(journal.read_text(encoding="utf-8"))
        atomic_write(directory / "thread.jsonl", data["thread"])
        atomic_write(directory / "meta.json", json.dumps(data["meta"], ensure_ascii=False))
        journal.unlink()

    # 持久化新用户消息及对应 Run 身份，先提交日志再生成兼容旧版的文件
    def begin_run(self, session: Session, content: str, run_id: str) -> None:
        self.recover(session.id)
        directory = self.session_dir(session.id)
        path = directory / "thread.jsonl"
        previous = path.read_text(encoding="utf-8") if path.exists() else ""
        row = {"ts": _now(), "role": "user", "content": content, "run_id": run_id}
        transaction = {
            "schema_version": 1,
            "meta": session.to_dict(),
            "thread": previous + json.dumps(row, ensure_ascii=False) + "\n",
        }
        atomic_write(
            directory / "session.transaction.json", json.dumps(transaction, ensure_ascii=False)
        )
        self.recover(session.id)

    # 返回指定 session 下的 runs 目录路径
    def runs_dir(self, sid: str) -> Path:
        return self.session_dir(sid) / "runs"

    # 扫描根目录下所有含 meta.json 的 session id 列表（按目录名排序，供启动恢复遍历）
    def list_session_ids(self) -> list[str]:
        return sorted(p.parent.name for p in self._root.glob("*/meta.json"))

    # 将 session meta 写入 meta.json
    def write_meta(self, session: Session) -> None:
        path = self.session_dir(session.id)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write(
            path / "meta.json", json.dumps(session.to_dict(), ensure_ascii=False, indent=2) + "\n"
        )

    # 从 meta.json 读取 session meta
    def read_meta(self, sid: str) -> Session:
        self.recover(sid)
        data = json.loads((self.session_dir(sid) / "meta.json").read_text(encoding="utf-8"))
        return Session.from_dict(data)

    # 追加一条 Anthropic API 消息到 thread.jsonl
    def append_message(
        self,
        sid: str,
        role: str,
        content: MessageContent,
        run_id: str | None = None,
    ) -> None:
        row: dict[str, Any] = {"ts": _now(), "role": role, "content": content}
        if run_id is not None:
            row["run_id"] = run_id
        path = self.session_dir(sid)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = path / "thread.jsonl"
        previous = target.read_text(encoding="utf-8") if target.exists() else ""
        atomic_write(target, previous + json.dumps(row, ensure_ascii=False) + "\n")

    # 批量追加一次 run 新产生的消息到 thread.jsonl
    def append_messages(
        self,
        sid: str,
        messages: list[dict[str, Any]],
        run_id: str,
        start_index: int | None = None,
    ) -> None:
        path = self.session_dir(sid) / "thread.jsonl"
        previous = path.read_text(encoding="utf-8") if path.exists() else ""
        rows = []
        existing: set[str] = set()
        for line in previous.splitlines():
            try:
                message_id = json.loads(line).get("message_id")
            except (json.JSONDecodeError, AttributeError):
                continue
            if isinstance(message_id, str):
                existing.add(message_id)
        for index, message in enumerate(messages):
            row = {
                "ts": _now(), "role": message["role"],
                "content": message["content"], "run_id": run_id,
            }
            if start_index is not None:
                message_id = f"{run_id}:{start_index + index}"
                if message_id in existing:
                    continue
                row["message_id"] = message_id
            rows.append(row)
        atomic_write(
            path, previous + "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        )

    # 恢复独立推理投影并附加其后新增的完整历史
    def read_messages(self, sid: str) -> list[dict[str, Any]]:
        self.recover(sid)
        history = self.read_history(sid)
        projection = ProjectionStore(self.session_dir(sid)).read()
        if projection is None:
            from claudedock.core.compact.budget import truncate_tool_results

            history = self._merge_consecutive_user(self._trim_orphan_tool_use(history))
            return truncate_tool_results(history)
        if projection.archive_count > len(history):
            raise ValueError("projection refers to missing transcript")
        combined = projection.messages + history[projection.archive_count:]
        return self._merge_consecutive_user(self._trim_orphan_tool_use(combined))

    # 读取完整审计历史，压缩不会覆盖或截断其中的正文
    def read_history(self, sid: str) -> list[dict[str, Any]]:
        self.recover(sid)
        path = self.session_dir(sid) / "thread.jsonl"
        if not path.exists():
            return []

        messages: list[dict[str, Any]] = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("skip broken thread row sid=%s line=%s", sid, line_no)
                continue
            role = row.get("role")
            if role not in ("user", "assistant"):
                logger.warning(
                    "skip unknown thread role sid=%s line=%s role=%s",
                    sid,
                    line_no,
                    role,
                )
                continue
            messages.append({"role": role, "content": row.get("content", "")})

        return messages

    # 裁掉尾部未配对 tool_use 以及其后的消息，避免 Anthropic messages.invalid
    def _trim_orphan_tool_use(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pending: set[str] = set()
        last_balanced = 0
        for idx, msg in enumerate(messages, start=1):
            content = msg.get("content")
            if isinstance(content, list):
                if msg.get("role") == "assistant":
                    for block in content:
                        if block.get("type") == "tool_use":
                            pending.add(str(block.get("id", "")))
                elif msg.get("role") == "user":
                    for block in content:
                        if block.get("type") == "tool_result":
                            pending.discard(str(block.get("tool_use_id", "")))
            if not pending:
                last_balanced = idx
        if pending:
            logger.warning("trim orphan tool_use blocks from thread")
            return messages[:last_balanced]
        return messages

    # 合并连续的同 role user 消息为一条，避免中断恢复后 thread 出现相邻 user 导致 API 报错
    def _merge_consecutive_user(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        for msg in messages:
            if merged and merged[-1]["role"] == "user" and msg["role"] == "user":
                prev_content = merged[-1]["content"]
                cur_content = msg["content"]
                if isinstance(prev_content, str) and isinstance(cur_content, str):
                    merged[-1]["content"] = prev_content + "\n\n" + cur_content
                elif isinstance(prev_content, list) and isinstance(cur_content, list):
                    merged[-1]["content"] = prev_content + cur_content
                else:
                    merged.append(msg)
                continue
            merged.append(msg)
        return merged

    # 判断 thread 是否以 user 消息结尾（无配对 assistant 收尾，说明上次 run 中断）
    def thread_ends_with_user(self, sid: str) -> bool:
        path = self.session_dir(sid) / "thread.jsonl"
        if not path.exists():
            return False
        last_role: str | None = None
        self.recover(sid)
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = row.get("role")
            if role in ("user", "assistant"):
                last_role = role
        return last_role == "user"

    # 兼容旧压缩入口，原子提交独立投影并保留完整 thread
    def write_compacted(self, sid: str, messages: list[dict[str, Any]]) -> int:
        projection = ProjectionStore(self.session_dir(sid)).commit(
            messages, len(self.read_history(sid)),
        )
        return projection.revision

    # 读取 notes.md 全文，文件不存在时返回空字符串
    def read_notes(self, sid: str) -> str:
        path = self.session_dir(sid) / "notes.md"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    # 将一条主动笔记追加到 notes.md
    def append_note(self, sid: str, content: str, run_id: str) -> None:
        path = self.session_dir(sid)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (path / "notes.md").open("a", encoding="utf-8") as f:
            f.write(f"## Note ({_now()}, {run_id})\n{content}\n\n")
