from __future__ import annotations

import asyncio
import fnmatch
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel

from kama_claude.core.bus.envelope import EventPushEnvelope
from kama_claude.core.trace.record import TraceRecord
from kama_claude.core.trace.writer import TraceWriter

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class _Subscription:
    sub_id: str
    writer: asyncio.StreamWriter
    topics: list[str]
    scope: str


class IpcEventBroadcaster:
    def __init__(self, trace: TraceWriter | None = None) -> None:
        self._subscriptions: list[_Subscription] = []
        self._run_sessions: dict[str, str] = {}
        self._active_runs: set[str] = set()
        self._trace = trace
        self._queues: dict[asyncio.StreamWriter, asyncio.Queue[bytes]] = {}
        self._senders: dict[asyncio.StreamWriter, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._gates: dict[asyncio.StreamWriter, asyncio.Event] = {}
        self._queued_bytes: dict[asyncio.StreamWriter, int] = {}

    # 将运行绑定到会话，客户端可选择仅订阅自己的会话
    def bind_run(self, run_id: str, session_id: str) -> None:
        self._run_sessions[run_id] = session_id
        if len(self._run_sessions) > 512:
            oldest = next((key for key in self._run_sessions if key not in self._active_runs), None)
            if oldest is not None:
                self._run_sessions.pop(oldest)

    # 注册一个客户端订阅，返回 subscription_id
    def subscribe(
        self,
        writer: asyncio.StreamWriter,
        topics: list[str],
        scope: str = "global",
        *,
        paused: bool = False,
    ) -> str:
        sub_id = f"sub-{uuid.uuid4().hex[:8]}"
        sub = _Subscription(sub_id=sub_id, writer=writer, topics=topics, scope=scope)
        self._subscriptions.append(sub)
        if writer not in self._queues:
            queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)
            self._queues[writer] = queue
            self._queued_bytes[writer] = 0
            gate = asyncio.Event()
            if not paused:
                gate.set()
            self._gates[writer] = gate
            task = asyncio.create_task(self._send_loop(writer, queue))
            self._senders[writer] = task
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return sub_id

    # 回放结束后才开始发送已缓存实时事件，保证历史先于新事件
    def resume(self, writer: asyncio.StreamWriter) -> None:
        gate = self._gates.get(writer)
        if gate is not None:
            gate.set()

    # 移除指定 writer 的所有订阅
    def unsubscribe(self, writer: asyncio.StreamWriter) -> None:
        self._subscriptions = [s for s in self._subscriptions if s.writer is not writer]
        self._queues.pop(writer, None)
        self._gates.pop(writer, None)
        self._queued_bytes.pop(writer, None)
        sender = self._senders.pop(writer, None)
        if sender is not None and sender is not asyncio.current_task():
            sender.cancel()

    # 独立发送每个连接的事件，慢消费者超时后断开且不阻塞事件总线
    async def _send_loop(self, writer: asyncio.StreamWriter, queue: asyncio.Queue[bytes]) -> None:
        try:
            while True:
                data = await queue.get()
                self._queued_bytes[writer] -= len(data)
                try:
                    await self._gates[writer].wait()
                    writer.write(data)
                    await asyncio.wait_for(writer.drain(), timeout=5.0)
                finally:
                    queue.task_done()
        except (ConnectionError, OSError, TimeoutError):
            writer.close()
        finally:
            if self._senders.get(writer) is asyncio.current_task():
                self.unsubscribe(writer)

    # 关闭所有发送任务并回收订阅队列
    async def aclose(self) -> None:
        for writer in list(self._queues):
            self.unsubscribe(writer)
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # 将事件推送到所有匹配的订阅客户端，写入失败时延迟清理死连接
    async def handle(self, event: BaseModel) -> None:
        event_dict = event.model_dump()
        event_type: str = event_dict.get("type", "")
        run_id: str | None = event_dict.get("run_id")

        if run_id and event_type in {"run.started", "subagent.started"}:
            self._active_runs.add(run_id)
        elif run_id and event_type in {"run.finished", "subagent.finished"}:
            self._active_runs.discard(run_id)
        if run_id and event_dict.get("session_id"):
            self.bind_run(run_id, event_dict["session_id"])
        parent = event_dict.get("parent_run_id")
        if run_id and parent in self._run_sessions:
            self.bind_run(run_id, self._run_sessions[parent])
        envelope = EventPushEnvelope(
            event=event_dict, sequence=getattr(event, "_event_sequence", 0)
        )
        payload = envelope.model_dump_json().encode() + b"\n"
        dead: list[asyncio.StreamWriter] = []

        owner = None
        if event_type.startswith("permission."):
            from kama_claude.core.transport.socket_server import get_connection_writer

            try:
                owner = get_connection_writer()
            except LookupError:
                pass
        for sub in list(self._subscriptions):
            if owner is not None and sub.writer is not owner:
                continue
            if not self._matches_topic(event_type, sub.topics):
                continue
            if not self._matches_scope(run_id, sub.scope, event_dict.get("session_id")):
                continue
            try:
                if self._queued_bytes[sub.writer] + len(payload) > 2 * 1024 * 1024:
                    raise asyncio.QueueFull
                self._queues[sub.writer].put_nowait(payload)
                self._queued_bytes[sub.writer] += len(payload)
                if self._trace is not None:
                    client_id = str(sub.writer.get_extra_info("peername", "<unknown>"))
                    self._trace.emit(
                        TraceRecord(
                            ts=_now(),
                            direction="CORE→CLIENT",
                            layer="ipc",
                            kind="push",
                            run_id=run_id,
                            client_id=client_id,
                            data={"sub_id": sub.sub_id, "event_type": event_type},
                        )
                    )
            except (asyncio.QueueFull, KeyError):
                sub.writer.close()
                logger.debug("dead connection for sub %s, scheduling cleanup", sub.sub_id)
                dead.append(sub.writer)

        for writer in dead:
            self.unsubscribe(writer)

    # 检查事件类型是否匹配订阅的 topic 列表（支持 fnmatch glob 模式）
    @staticmethod
    def _matches_topic(event_type: str, topics: list[str]) -> bool:
        return any(fnmatch.fnmatch(event_type, pattern) for pattern in topics)

    # 检查事件 run_id 是否匹配订阅的 scope（global 全通，run:<id> 精确匹配）
    def _matches_scope(self, run_id: str | None, scope: str, session_id: str | None = None) -> bool:
        if scope == "global":
            return True
        if scope.startswith("session:"):
            expected = scope[8:]
            return session_id == expected or self._run_sessions.get(run_id or "") == expected
        if scope.startswith("run:"):
            return run_id == scope[4:]
        return False
