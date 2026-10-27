from __future__ import annotations

import asyncio
import hashlib
import logging
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claudedock.core.bus.events import ContextMemoryUpdatedEvent
from claudedock.core.session.memory import SessionNoteStore

if TYPE_CHECKING:
    from claudedock.core.events.bus import EventBus
    from claudedock.core.supervisor import RunSupervisor

log = logging.getLogger(__name__)


class SessionMemoryService:
    # 每会话复用确定性增量状态，后台保存失败不阻塞主任务
    def __init__(
        self, directory: Path, session_id: str, workspace: Path,
        supervisor: RunSupervisor | None = None,
    ) -> None:
        self.store = SessionNoteStore(directory)
        self.state = self.store.read()
        self.session_id = session_id
        self.workspace = workspace.resolve()
        self.supervisor = supervisor
        self._params: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self._task: asyncio.Task[None] | None = None

    # 从可信事件抽取请求与已执行证据，绝不从助手正文推断完成状态
    async def observe(self, event: Any, bus: EventBus) -> None:
        kind = event.type
        if kind not in {
            "run.started", "tool.call_started", "tool.call_finished",
            "tool.call_failed", "run.finished",
        }:
            return
        run_id = getattr(event, "run_id", "")
        if kind == "run.started":
            goal = str(event.goal)[:2048]
            if not self.state.goal:
                self.state.goal = goal
            self.state.active_goal = goal
            self.state.requests = (self.state.requests + [goal])[-16:]
            self.state.pending = ["Review task completion against the latest user request"]
            self.state.source_run_id = run_id
            self._params.clear()
        if run_id != self.state.source_run_id:
            return
        self.state.updated_at = event.ts
        if kind == "tool.call_started":
            self._params[event.tool_use_id] = dict(event.params)
        elif kind == "tool.call_finished":
            self.state.completed_verified.append({
                "run_id": run_id, "tool_use_id": event.tool_use_id,
                "tool_name": event.tool_name, "status": "executed_successfully",
                "output_digest": hashlib.sha256(event.output.encode("utf-8")).hexdigest(),
            })
            self.state.completed_verified = self.state.completed_verified[-64:]
            self._record_file(event.tool_use_id, event.tool_name)
        elif kind == "tool.call_failed":
            self.state.known_failures.append({
                "run_id": run_id, "tool_use_id": event.tool_use_id,
                "error_class": event.error_class,
            })
            self.state.known_failures = self.state.known_failures[-32:]
        elif kind == "run.finished":
            self.state.open_questions = (
                ["Run ended; final task claims still require evidence review"]
                if event.status == "success"
                else ["Resolve interrupted or failed work before resume"]
            )
        identity = f"{run_id}:{kind}:{getattr(event, 'tool_use_id', '')}:{event.ts}"
        self.state.source_event_ids = (
            self.state.source_event_ids + [hashlib.sha256(identity.encode()).hexdigest()[:24]]
        )[-128:]
        self.state.updated_at = event.ts
        self._dirty = True
        if self._task is None or self._task.done():
            work = self.flush(bus)
            if self.supervisor is not None:
                try:
                    self._task = self.supervisor.spawn(
                        work, key=f"session-memory:{self.session_id}",
                    )
                except RuntimeError:
                    log.warning("session memory scheduling deferred")
            else:
                self._task = asyncio.create_task(work)

    # 只在成功文件操作后记录工作区内可核验的小文件摘要
    def _record_file(self, tool_id: str, tool_name: str) -> None:
        params = self._params.pop(tool_id, {})
        if tool_name not in {"read_file", "write_file"}:
            return
        try:
            path = Path(str(params["path"])).resolve(strict=True)
            if not path.is_relative_to(self.workspace) or not path.is_file():
                return
            if path.stat().st_size > 2 * 1024 * 1024:
                return
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.state.files_touched[str(path.relative_to(self.workspace))] = {
                "digest": digest, "tool_use_id": tool_id, "verified_at": self.state.updated_at,
            }
            while len(self.state.files_touched) > 64:
                self.state.files_touched.pop(next(iter(self.state.files_touched)))
        except (OSError, KeyError, ValueError):
            return

    # 在异步调度边界生成快照，用 CAS 拒绝过期写回
    async def flush(self, bus: EventBus) -> None:
        await asyncio.sleep(0)
        while self._dirty:
            self._dirty = False
            candidate = deepcopy(self.state)
            expected = candidate.revision
            try:
                if not self.store.compare_and_swap(candidate, expected):
                    log.warning("stale session memory candidate rejected")
                    return
                self.state.revision = candidate.revision
                await bus.publish(ContextMemoryUpdatedEvent(
                    session_id=self.session_id, run_id=candidate.source_run_id,
                    revision=candidate.revision, source_count=len(candidate.source_event_ids),
                    ts=candidate.updated_at,
                ))
            except (OSError, ValueError):
                log.exception("session memory persistence failed")
                return

    # 关闭时等待笔记任务，保持取消传播与任务回收明确
    async def aclose(self) -> None:
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
