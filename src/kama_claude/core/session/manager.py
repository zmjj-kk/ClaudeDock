from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from kama_claude.core.bus.envelope import HandlerError
from kama_claude.core.bus.events import (
    RunFinishedEvent,
    SessionClosedEvent,
    SessionCreatedEvent,
    SessionMessageReceivedEvent,
    SessionResumedEvent,
    SessionWaitingForInputEvent,
    SkillInvokedEvent,
)
from kama_claude.core.compact.projection import ProjectionStore
from kama_claude.core.context import ExecutionBudget, ExecutionContext, current_budget
from kama_claude.core.events.bus import EventBus
from kama_claude.core.journal import RunJournal
from kama_claude.core.runs import new_run_id
from kama_claude.core.session.model import Session, SessionMode
from kama_claude.core.session.store import SessionStore
from kama_claude.core.skills.loader import SkillLoader

if TYPE_CHECKING:
    from kama_claude.core.llm.base import LLMProvider
    from kama_claude.core.runner import AgentRunner

SESSION_NOT_FOUND = -32010
SESSION_CLOSED = -32011
SESSION_BUSY = -32012

logger = logging.getLogger(__name__)


@dataclass
class RestoreResult:
    restored: int  # 成功重建进内存的 session 数
    skipped: int  # 因 meta 损坏被跳过的 session 数
    interrupted: int  # 检测到上次 run 中断（thread 以 user 结尾）的 session 数


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class SessionManager:
    # 初始化会话管理器，接入文件存储、runner 工厂、事件总线和可选的 LLM provider（用于手动压缩）
    def __init__(
        self,
        store: SessionStore,
        runner_factory: Callable[[], AgentRunner],
        bus: EventBus,
        provider: LLMProvider | None = None,
    ) -> None:
        self._store = store
        self._runner_factory = runner_factory
        self._bus = bus
        self._provider = provider
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._skill_loader = SkillLoader()
        self._restored = False

    # 创建新 session 并写入 meta.json
    async def create(self, mode: SessionMode, title: str = "") -> Session:
        sid = f"sess-{uuid.uuid4().hex[:12]}"
        ts = _now()
        session = Session(
            id=sid,
            mode=mode,
            status="active",
            title=title,
            created_at=ts,
            updated_at=ts,
            run_ids=[],
        )
        self._sessions[sid] = session
        self._locks[sid] = asyncio.Lock()
        self._store.write_meta(session)
        await self._bus.publish(SessionCreatedEvent(session_id=sid, mode=mode, ts=ts))
        return session

    # 扫描磁盘恢复未关闭 session；损坏 meta 记警告跳过，检测中断 run 并补发取消事件，返回结构化结果
    async def restore(self) -> RestoreResult:
        if self._restored:
            return RestoreResult(restored=0, skipped=0, interrupted=0)
        self._restored = True

        # 磁盘扫描放到线程池，避免大量 session 文件阻塞事件循环
        def _scan() -> tuple[list[Session], int]:
            found: list[Session] = []
            skipped = 0
            for sid in self._store.list_session_ids():
                try:
                    session = self._store.read_meta(sid)
                except Exception:
                    logger.warning("skip broken session meta sid=%s", sid)
                    skipped += 1
                    continue
                if session.status == "closed":
                    continue
                found.append(session)
            return found, skipped

        sessions, skipped = await asyncio.to_thread(_scan)
        interrupted = 0
        for session in sessions:
            self._sessions[session.id] = session
            self._locks[session.id] = asyncio.Lock()
            checkpoint_state = ""
            if session.run_ids:
                path = self._store.runs_dir(session.id) / session.run_ids[-1] / "checkpoint.json"
                if path.exists():
                    try:
                        journal = RunJournal(path, session.run_ids[-1], "")
                        checkpoint_state = journal.data.get("state", "")
                        messages = journal.data.get("messages")
                        if (
                            isinstance(messages, list)
                            and messages
                            and checkpoint_state
                            in {
                                "queued",
                                "running",
                                "waiting_approval",
                                "cancelling",
                                "interrupted",
                            }
                        ):
                            audit = journal.data.get("audit_messages")
                            start = journal.data.get("audit_start")
                            if isinstance(audit, list) and isinstance(start, int):
                                self._store.append_messages(
                                    session.id, audit[start:], session.run_ids[-1],
                                    start_index=start,
                                )
                            committed = ProjectionStore(self._store.session_dir(session.id)).read()
                            checkpoint_revision = journal.data.get("projection_revision", 0)
                            if committed is None or checkpoint_revision >= committed.revision:
                                self._store.write_compacted(
                                    session.id, self._store._trim_orphan_tool_use(messages),
                                )
                        if checkpoint_state in {
                            "queued",
                            "running",
                            "waiting_approval",
                            "cancelling",
                        }:
                            journal.data["state"] = "interrupted"
                            journal.save()
                    except (ValueError, OSError, json.JSONDecodeError):
                        logger.warning("invalid run checkpoint sid=%s", session.id)
            if checkpoint_state in {
                "queued",
                "running",
                "waiting_approval",
                "cancelling",
                "interrupted",
            } or self._store.thread_ends_with_user(session.id):
                interrupted += 1
                if session.run_ids:
                    await self._bus.publish(
                        RunFinishedEvent(
                            run_id=session.run_ids[-1],
                            status="failed",
                            reason="interrupted",
                            steps=0,
                            ts=_now(),
                        )
                    )
                if session.status != "waiting_for_input":
                    session.status = "waiting_for_input"
                    self._store.write_meta(session)
        return RestoreResult(restored=len(sessions), skipped=skipped, interrupted=interrupted)

    # 处理用户消息，追加 thread 并启动一次 agent run
    async def send_message(self, sid: str, content: str, *, run_id: str | None = None) -> str:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")

        async with lock:
            if session.status == "closed":
                raise HandlerError(SESSION_CLOSED, "session already closed")

            if session.status == "waiting_for_input":
                await self._bus.publish(SessionResumedEvent(session_id=sid, ts=_now()))

            if not session.title:
                session.title = content[:40]

            run_id = run_id or new_run_id()
            session.run_ids.append(run_id)
            session.updated_at = _now()
            self._store.begin_run(session, content, run_id)
            await self._bus.publish(
                SessionMessageReceivedEvent(session_id=sid, content=content, ts=_now())
            )

            # Skill 解析：检测 "/" 前缀，展开为系统提示覆盖和工具白名单
            goal = content
            system_prompt_override: str | None = None
            tool_whitelist: list[str] | None = None
            if content.startswith("/"):
                parts = content[1:].split(None, 1)
                skill_name = parts[0]
                arguments = parts[1] if len(parts) > 1 else ""
                skill = self._skill_loader.resolve(skill_name)
                if skill is not None:
                    goal = self._skill_loader.render_prompt(skill, arguments)
                    system_prompt_override = skill.system_prompt_template
                    tool_whitelist = skill.allowed_tools or None
                    await self._bus.publish(
                        SkillInvokedEvent(
                            skill_name=skill_name,
                            arguments=arguments,
                            run_id=run_id,
                            ts=_now(),
                        )
                    )

            runner = self._runner_factory()
            try:
                await runner.run_and_capture(
                    goal,
                    run_id=run_id,
                    session=session,
                    store=self._store,
                    system_prompt_override=system_prompt_override,
                    tool_whitelist=tool_whitelist,
                )
            except asyncio.CancelledError:
                session.status = "waiting_for_input"
                session.updated_at = _now()
                self._store.write_meta(session)
                await self._bus.publish(
                    SessionWaitingForInputEvent(
                        session_id=sid,
                        last_run_id=run_id,
                        ts=session.updated_at,
                    )
                )
                raise

            session.updated_at = _now()
            if session.mode == "one_shot":
                session.status = "closed"
                await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))
            else:
                session.status = "waiting_for_input"
                await self._bus.publish(
                    SessionWaitingForInputEvent(
                        session_id=sid,
                        last_run_id=run_id,
                        ts=session.updated_at,
                    )
                )
            self._store.write_meta(session)
            return run_id

    # 关闭指定 session 并更新 meta.json
    async def close(self, sid: str) -> None:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        async with lock:
            session.status = "closed"
            session.updated_at = _now()
            self._store.write_meta(session)
            await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))

    # 手动滚动压缩推理投影，保留近期原文与完整审计历史
    async def compact(self, sid: str, focus: str = "") -> Any:
        self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        if self._provider is None:
            raise HandlerError(-32020, "provider not available for compaction")
        async with lock:
            from kama_claude.core.bus.commands import SessionCompactResult
            from kama_claude.core.compact.compactor import Compactor

            messages = self._store.read_messages(sid)
            session_dir = self._store.session_dir(sid)
            # 提交会话投影并同步修订号，保持回调的兼容签名
            def commit(candidate: list[dict[str, Any]]) -> None:
                context.projection_revision = self._store.write_compacted(sid, candidate)

            compactor = Compactor(
                self._bus, session_dir, sid, checkpoint=commit,
                recent_turns=4, enforce_gain=True,
            )
            context = ExecutionContext(
                run_id=f"compact-{uuid.uuid4().hex}", goal="Preserve the user's task",
                max_steps=1, prefill_messages=messages,
                user_requests=[
                    message["content"] for message in self._store.read_history(sid)
                    if message["role"] == "user" and isinstance(message.get("content"), str)
                ],
            )
            existing_projection = ProjectionStore(session_dir).read()
            context.projection_revision = (
                existing_projection.revision if existing_projection is not None else 0
            )
            token = current_budget.set(ExecutionBudget(max_model_calls=1))
            try:
                async with asyncio.timeout(120):
                    result = await compactor.compact(
                        context, self._provider, focus=focus, hard_limit=True,
                    )
            finally:
                current_budget.reset(token)
            if result is None:
                raise HandlerError(-32021, "compaction failed or not beneficial")
            return SessionCompactResult(
                summary_tokens=result.summary_tokens,
                projection_tokens=result.projection_tokens,
                revision=context.projection_revision,
                retained_messages=result.retained_messages,
                saved_tokens=max(
                    0, result.original_token_estimate
                    - (result.projection_tokens or result.summary_tokens),
                ),
            )

    # 查看脱敏后的辅助笔记、规则来源与投影元数据，不返回隐藏正文
    async def get_context(self, sid: str) -> dict[str, Any]:
        from kama_claude.core.security.redaction import redact
        from kama_claude.core.session.memory import SessionNoteStore

        session = self._get_session(sid)
        directory = self._store.session_dir(sid)
        notes = "\n\n".join((
            str(redact(self._store.read_notes(sid)[:8192])),
            SessionNoteStore(directory).read().render(32768),
        )).strip()
        projection = ProjectionStore(directory).read()
        sources: list[dict[str, Any]] = []
        if session.run_ids:
            path = self._store.runs_dir(sid) / session.run_ids[-1] / "checkpoint.json"
            if path.exists():
                checkpoint = json.loads(path.read_text(encoding="utf-8"))
                sources = checkpoint.get("instruction_sources", [])
        return {
            "notes": notes, "sources": redact(sources),
            "projection": {
                "revision": projection.revision, "archive_count": projection.archive_count,
                "digest": projection.digest,
            } if projection is not None else {},
        }

    # 读取指定 session 的完整 thread 历史
    async def get_history(self, sid: str) -> list[dict[str, Any]]:
        self._get_session(sid)
        return self._store.read_history(sid)

    # 返回当前内存索引中的全部 session（含恢复的），供客户端列出可续接会话
    def list_sessions(self) -> list[Session]:
        return list(self._sessions.values())

    # 从内存索引取 session，不存在时抛 JSON-RPC 结构化错误
    def _get_session(self, sid: str) -> Session:
        session = self._sessions.get(sid)
        if session is None:
            raise HandlerError(SESSION_NOT_FOUND, "session not found")
        return session
