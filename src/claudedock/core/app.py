from __future__ import annotations

import asyncio
import datetime
import fnmatch
import json
import logging
import os
import signal
import ssl
import time
from contextlib import AsyncExitStack
from datetime import UTC
from pathlib import Path
from typing import Any

from pydantic import BaseModel

import claudedock
from claudedock.core.bus.commands import (
    AgentRunCommand,
    AgentRunResult,
    EventSubscribeCommand,
    EventSubscribeResult,
    MemoryConsolidateCommand,
    MemoryConsolidateResult,
    PermissionRespondCommand,
    PermissionRespondResult,
    PongResult,
    RunCancelCommand,
    RunCancelResult,
    RunGetCommand,
    RunGetResult,
    SessionCloseCommand,
    SessionCloseResult,
    SessionCompactCommand,
    SessionCompactResult,
    SessionCreateCommand,
    SessionCreateResult,
    SessionGetContextCommand,
    SessionGetContextResult,
    SessionGetHistoryCommand,
    SessionGetHistoryResult,
    SessionListCommand,
    SessionListItem,
    SessionListResult,
    SessionSendMessageCommand,
    SessionSendMessageResult,
)
from claudedock.core.bus.envelope import EventPushEnvelope, HandlerError
from claudedock.core.config import ClaudeDockConfig, get_config
from claudedock.core.daemon_lock import DaemonLock
from claudedock.core.events.bus import EventBus
from claudedock.core.llm.base import LLMProvider
from claudedock.core.llm.provider import AnthropicProvider
from claudedock.core.logging_setup import setup_logging
from claudedock.core.mcp.server import McpServerManager
from claudedock.core.memory.factory import build_memory_store
from claudedock.core.memory.store import MemoryStore
from claudedock.core.monitoring.context_metrics import observe_context
from claudedock.core.monitoring.otel import setup_otel, shutdown_otel
from claudedock.core.permissions.manager import PermissionManager
from claudedock.core.permissions.rules import load_rules
from claudedock.core.permissions.storage import load_policy_file
from claudedock.core.runner import AgentRunner
from claudedock.core.runs import events_file, new_run_id
from claudedock.core.security.classifier import build_classifier
from claudedock.core.security.decision_audit import SecurityAudit
from claudedock.core.session import SessionManager, SessionStore
from claudedock.core.supervisor import RunSupervisor
from claudedock.core.trace.record import TraceRecord
from claudedock.core.trace.writer import TraceWriter
from claudedock.core.transport.auth import server_token
from claudedock.core.transport.ipc_broadcaster import IpcEventBroadcaster
from claudedock.core.transport.socket_server import SocketServer, get_connection_writer

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.datetime.now(UTC).isoformat()


class CoreApp:
    def __init__(self) -> None:
        self.phase = "initializing"
        self._server: SocketServer | None = None
        self._supervisor = RunSupervisor()
        self._start_time = time.monotonic()
        self._bus = EventBus()
        self._broadcaster: IpcEventBroadcaster | None = None
        self._trace: TraceWriter | None = None
        self._config: ClaudeDockConfig | None = None
        self._running_runs: set[asyncio.Task[Any]] = set()
        self._sessions: SessionManager | None = None
        self._permission_manager: PermissionManager | None = None
        self._mcp_manager: McpServerManager | None = None
        self._memory_store: MemoryStore | None = None
        self._provider: LLMProvider | None = None
        # 连接 writer → 该连接上创建的 session id 集合，用于断连时取消待审批请求
        self._writer_sessions: dict[asyncio.StreamWriter, set[str]] = {}

    # 处理 core.ping 请求，返回服务版本、运行时长和接收时间
    async def _ping_handler(self, params: dict[str, Any]) -> PongResult:
        client = params.get("client", "unknown")
        logger.debug("ping from %s", client)
        return PongResult(
            server_version=claudedock.__version__,
            uptime_ms=int((time.monotonic() - self._start_time) * 1000),
            received_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )

    # 将 EventBus 事件写入 trace（作为 EventBus 订阅者）
    async def _trace_event_handler(self, event: BaseModel) -> None:
        assert self._trace is not None
        event_dict = event.model_dump()
        self._trace.emit(
            TraceRecord(
                ts=_now(),
                direction="CORE",
                layer="event",
                kind="event",
                run_id=event_dict.get("run_id"),
                data=event_dict,
            )
        )

    # 将当前连接的 writer 与其驱动的 session 绑定，断连时据此取消待审批请求
    def _remember_session_writer(self, session_id: str) -> None:
        try:
            writer = get_connection_writer()
        except LookupError:
            return
        self._writer_sessions.setdefault(writer, set()).add(session_id)

    # 客户端断连时拒绝该连接上所有 session 的待审批请求，防止 Future 永久挂起
    async def _on_client_disconnect(self, writer: asyncio.StreamWriter) -> None:
        session_ids = self._writer_sessions.pop(writer, set())
        if self._permission_manager is not None:
            self._permission_manager.cancel_owner(writer)
        if not session_ids or self._permission_manager is None:
            return
        # 审批已按连接取消，不按共享会话影响其他连接

    # 启动一次 agent run：异步创建 AgentRunner 并立即返回 run_id
    async def _agent_run_handler(self, params: dict[str, Any]) -> AgentRunResult:
        assert self._sessions is not None
        cmd = AgentRunCommand.model_validate(params)
        session = await self._sessions.create(mode="one_shot", title=cmd.goal[:40])
        self._remember_session_writer(session.id)
        run_id = new_run_id()
        assert self._broadcaster is not None
        self._broadcaster.bind_run(run_id, session.id)
        run_task = self._supervisor.spawn(
            self._supervisor.run(
                self._sessions.send_message(session.id, cmd.goal, run_id=run_id), run_id=run_id
            )
        )
        self._running_runs.add(run_task)
        run_task.add_done_callback(self._running_runs.discard)
        return AgentRunResult(run_id=run_id)

    # 查询活动运行状态，重启后回退读取版本化检查点
    async def _run_get_handler(self, params: dict[str, Any]) -> RunGetResult:
        cmd = RunGetCommand.model_validate(params)
        state = self._supervisor.state(cmd.run_id)
        if state is not None:
            return RunGetResult(run_id=cmd.run_id, state=state)
        assert self._config is not None
        root = Path(self._config.sessions_dir).expanduser()
        for path in root.glob(f"*/runs/{cmd.run_id}/checkpoint.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            return RunGetResult(
                run_id=cmd.run_id, state=data.get("state", "interrupted"), reason=data.get("reason")
            )
        raise HandlerError(-32030, "run not found")

    # 取消运行并由 Supervisor 传播至子任务与工具进程
    async def _run_cancel_handler(self, params: dict[str, Any]) -> RunCancelResult:
        cmd = RunCancelCommand.model_validate(params)
        return RunCancelResult(cancelled=self._supervisor.cancel(cmd.run_id))

    # 创建 chat 或 one_shot session，并返回 session_id
    async def _session_create_handler(self, params: dict[str, Any]) -> SessionCreateResult:
        assert self._sessions is not None
        cmd = SessionCreateCommand.model_validate(params)
        session = await self._sessions.create(mode=cmd.mode, title=cmd.title)
        self._remember_session_writer(session.id)
        return SessionCreateResult(session_id=session.id, status=session.status)

    # 向 session 发送一条用户消息并同步等待对应 run 完成
    async def _session_send_handler(self, params: dict[str, Any]) -> SessionSendMessageResult:
        assert self._sessions is not None
        cmd = SessionSendMessageCommand.model_validate(params)
        self._remember_session_writer(cmd.session_id)
        run_id = new_run_id()
        assert self._broadcaster is not None
        self._broadcaster.bind_run(run_id, cmd.session_id)
        await self._supervisor.run(
            self._sessions.send_message(cmd.session_id, cmd.content, run_id=run_id), run_id=run_id
        )
        return SessionSendMessageResult(run_id=run_id)

    # 返回当前会话脱敏后的上下文来源与笔记视图
    async def _session_context_handler(self, params: dict[str, Any]) -> SessionGetContextResult:
        assert self._sessions is not None
        command = SessionGetContextCommand.model_validate(params)
        return SessionGetContextResult(**await self._sessions.get_context(command.session_id))

    # 返回 session 的完整 Anthropic messages 历史
    async def _session_history_handler(self, params: dict[str, Any]) -> SessionGetHistoryResult:
        assert self._sessions is not None
        cmd = SessionGetHistoryCommand.model_validate(params)
        messages = await self._sessions.get_history(cmd.session_id)
        return SessionGetHistoryResult(messages=messages)

    # 返回当前内存中所有 session 的摘要列表（含启动恢复的旧 session），供客户端选择续接
    async def _session_list_handler(self, params: dict[str, Any]) -> SessionListResult:
        assert self._sessions is not None
        SessionListCommand.model_validate(params)
        items = [
            SessionListItem(
                id=s.id,
                title=s.title,
                status=s.status,
                mode=s.mode,
                created_at=s.created_at,
                updated_at=s.updated_at,
            )
            for s in self._sessions.list_sessions()
        ]
        return SessionListResult(sessions=items)

    # 接收客户端权限审批响应，resolve 对应挂起的 Future
    async def _permission_respond_handler(self, params: dict[str, Any]) -> PermissionRespondResult:
        cmd = PermissionRespondCommand.model_validate(params)
        logger.info(
            "permission.respond received tool_use_id=%s decision=%s",
            cmd.tool_use_id,
            cmd.decision,
        )
        if self._permission_manager is None:
            logger.error("permission.respond: PermissionManager not initialized")
            return PermissionRespondResult()
        self._permission_manager.respond(
            cmd.tool_use_id,
            cmd.decision,
            owner=get_connection_writer(),
            approval_token=cmd.approval_token,
        )
        return PermissionRespondResult()

    # 手动压缩 session thread，将摘要持久化写入 thread.jsonl
    async def _session_compact_handler(self, params: dict[str, Any]) -> SessionCompactResult:
        assert self._sessions is not None
        cmd = SessionCompactCommand.model_validate(params)
        result = await self._sessions.compact(cmd.session_id, cmd.focus)
        return result  # type: ignore[no-any-return]

    # 手动触发长期记忆整合：去重/冲突消解/抽象提炼，返回合并与退休条数
    async def _memory_consolidate_handler(self, params: dict[str, Any]) -> MemoryConsolidateResult:
        MemoryConsolidateCommand.model_validate(params)
        if self._memory_store is None:
            logger.error("memory.consolidate: MemoryStore not initialized")
            return MemoryConsolidateResult()
        assert self._config is not None
        if self._provider is None:
            raise HandlerError(-32020, "model provider unavailable")
        async with asyncio.timeout(self._config.runtime.run_timeout_s):
            result = await self._memory_store.consolidate(self._provider)
        if result is None:
            return MemoryConsolidateResult()
        return MemoryConsolidateResult(merged=len(result.replace), retired=len(result.retire))

    # 关闭 session 并返回 closed 状态
    async def _session_close_handler(self, params: dict[str, Any]) -> SessionCloseResult:
        assert self._sessions is not None
        cmd = SessionCloseCommand.model_validate(params)
        await self._sessions.close(cmd.session_id)
        if self._permission_manager is not None:
            self._permission_manager.revoke_session(cmd.session_id)
        return SessionCloseResult(status="closed")

    # 注册客户端事件订阅，可选先回放 events.jsonl 历史再接收实时流
    async def _subscribe_handler(self, params: dict[str, Any]) -> EventSubscribeResult:
        cmd = EventSubscribeCommand.model_validate(params)
        writer = get_connection_writer()

        assert self._broadcaster is not None
        self._broadcaster.unsubscribe(writer)
        sub_id = self._broadcaster.subscribe(writer, cmd.topics, cmd.scope, paused=True)
        replayed_count = 0
        try:
            if cmd.replay_from_run is not None:
                replayed_count = await self._replay_events(
                    cmd.replay_from_run, writer, cmd.topics, cmd.after_sequence
                )
        finally:
            self._broadcaster.resume(writer)
        return EventSubscribeResult(subscription_id=sub_id, replayed_count=replayed_count)

    # 从 events.jsonl 向 writer 回放匹配 topic 的历史事件，返回已回放条数
    async def _replay_events(
        self,
        run_id: str,
        writer: asyncio.StreamWriter,
        topics: list[str],
        after_sequence: int = 0,
    ) -> int:
        path = events_file(run_id)
        if not path.exists():
            assert self._config is not None
            for candidate in (
                Path(self._config.sessions_dir).expanduser().glob(f"*/runs/{run_id}/events.jsonl")
            ):
                path = candidate
                break
        if not path.exists():
            return 0

        count = 0
        for line in path.read_text().splitlines():
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            sequence = event.pop("_sequence", 0)
            if sequence and sequence <= after_sequence:
                continue
            event["_replay"] = True
            event_type: str = event.get("type", "")
            if not any(fnmatch.fnmatch(event_type, p) for p in topics):
                continue
            envelope = EventPushEnvelope(event=event, sequence=sequence)
            writer.write(envelope.model_dump_json().encode() + b"\n")
            await asyncio.wait_for(writer.drain(), timeout=5.0)
            count += 1

        if count:
            await writer.drain()
        return count

    # 启动守护进程：加载配置、初始化日志、启动 trace、启动 TCP 服务器，并等待退出信号
    async def run(self) -> None:
        self.phase = "initializing"
        try:
            async with AsyncExitStack() as stack:
                await self._serve(stack)
        finally:
            self.phase = "closed"

    # 初始化共享依赖并登记逆序清理，启动失败也释放已创建资源
    async def _serve(self, stack: AsyncExitStack) -> None:
        self._start_time = time.monotonic()
        self._config = get_config()
        setup_logging(self._config)
        setup_otel()
        stack.callback(shutdown_otel)
        if self._config.host not in {"127.0.0.1", "::1", "localhost"}:
            if not (
                os.environ.get("CLAUDEDOCK_IPC_TOKEN")
                and os.environ.get("CLAUDEDOCK_ALLOW_REMOTE") == "1"
            ):
                raise SystemExit(
                    "Non-loopback IPC requires CLAUDEDOCK_IPC_TOKEN and CLAUDEDOCK_ALLOW_REMOTE=1"
                )
        self._supervisor = RunSupervisor(
            self._config.runtime.max_runs, self._config.runtime.max_jobs
        )
        stack.callback(self._bus.subscribe(self._supervisor.observe))
        stack.callback(self._bus.subscribe(observe_context))
        tls_context = None
        if self._config.tls_cert and self._config.tls_key:
            tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
            tls_context.load_cert_chain(self._config.tls_cert, self._config.tls_key)
        elif (
            self._config.host not in {"127.0.0.1", "::1", "localhost"}
            and os.environ.get("CLAUDEDOCK_TRUSTED_PROXY") != "1"
        ):
            raise SystemExit("Remote IPC requires TLS or an explicit trusted private proxy")
        token = server_token(self._config.port)

        if self._config.trace.enabled:
            trace_path = Path(self._config.trace.file).expanduser()
            self._trace = TraceWriter(
                trace_path, include_payload=self._config.trace.include_llm_payload
            )
            await self._trace.start()
            stack.push_async_callback(self._trace.stop)
            stack.callback(self._bus.subscribe(self._trace_event_handler))

        policy_file = Path("~/.claudedock/policy.toml").expanduser()
        self._permission_manager = PermissionManager(
            policy_file=policy_file,
            timeout_s=self._config.permission.timeout_s,
            mode=self._config.permission.mode,
            bypass_enabled=self._config.permission.bypass_enabled,
            classifier=build_classifier(self._config.permission),
            audit=SecurityAudit(Path("~/.claudedock/logs/security.jsonl").expanduser()),
            rules=load_rules(
                (
                    (Path("/etc/claudedock/policy.toml"), "global"),
                    (policy_file, "user"),
                    (Path.cwd() / ".claudedock/policy.toml", "project"),
                )
            ),
        )
        logger.info(
            "permission manager: timeout_s=%.1f  persistent=%d entries",
            self._config.permission.timeout_s,
            len(load_policy_file(policy_file)),
        )

        self._broadcaster = IpcEventBroadcaster(trace=self._trace)
        stack.push_async_callback(self._broadcaster.aclose)
        stack.callback(self._bus.subscribe(self._broadcaster.handle))
        sessions_root = Path(self._config.sessions_dir).expanduser()
        directory_lock = DaemonLock(sessions_root)
        stack.callback(directory_lock.close)
        store = SessionStore(sessions_root)
        assert self._config is not None
        compact_provider = None
        if os.environ.get("ANTHROPIC_API_KEY"):
            compact_provider = AnthropicProvider(self._config.llm.default_model)
            stack.push_async_callback(compact_provider.aclose)

        self._mcp_manager = McpServerManager()
        stack.push_async_callback(self._mcp_manager.stop_all)
        if self._config.mcp.servers:
            logger.info("mcp: starting %d server(s)", len(self._config.mcp.servers))
            await self._mcp_manager.start_all(self._config.mcp.servers)

        # 长期记忆单例：配置启用时构建并加载两层存储；失败降级为禁用，不阻断 daemon 启动
        if self._config.memory.enabled:
            try:
                self._memory_store = build_memory_store(self._config.memory, self._config.rag)
                stack.push_async_callback(self._memory_store.aclose)
                await self._memory_store.load()
                logger.info("memory: loaded %d entries", self._memory_store.count())
            except Exception:
                logger.exception("memory: build/load failed, memory disabled")
                self._memory_store = None

        runner = AgentRunner(
            self._config,
            bus=self._bus,
            trace=self._trace,
            provider=compact_provider,
            permission_manager=self._permission_manager,
            mcp_manager=self._mcp_manager,
            memory_store=self._memory_store,
            auto_reload_rag=True,
            supervisor=self._supervisor,
        )
        self._provider = runner.provider
        stack.push_async_callback(runner.aclose)
        self._sessions = SessionManager(
            store,
            runner_factory=lambda: runner,
            bus=self._bus,
            provider=runner.provider,
        )

        # 启动时恢复磁盘上未关闭的 session，避免重启后 thread 沦为孤儿文件
        result = await self._sessions.restore()
        for session in self._sessions.list_sessions():
            for run_id in session.run_ids[-128:]:
                self._broadcaster.bind_run(run_id, session.id)
        if result.restored:
            logger.info(
                "session recovery: restored=%d skipped=%d interrupted=%d",
                result.restored,
                result.skipped,
                result.interrupted,
            )

        server = SocketServer(
            self._config.host,
            self._config.port,
            self._broadcaster,
            trace=self._trace,
            on_disconnect=self._on_client_disconnect,
            auth_token=token,
            ssl_context=tls_context,
            max_connections=self._config.runtime.max_connections,
            max_requests=self._config.runtime.max_requests,
            request_timeout=self._config.runtime.request_timeout_s,
        )
        self._server = server
        self.phase = "ready"
        server.register("core.ping", self._ping_handler)
        server.register("run.get", self._run_get_handler)
        server.register("run.cancel", self._run_cancel_handler)
        server.register("agent.run", self._agent_run_handler)
        server.register("event.subscribe", self._subscribe_handler)
        server.register("session.create", self._session_create_handler)
        server.register("session.send_message", self._session_send_handler)
        server.register("session.get_context", self._session_context_handler)
        server.register("session.get_history", self._session_history_handler)
        server.register("session.list", self._session_list_handler)
        server.register("session.close", self._session_close_handler)
        server.register("permission.respond", self._permission_respond_handler)
        server.register("session.compact", self._session_compact_handler)
        server.register("memory.consolidate", self._memory_consolidate_handler)

        stack.push_async_callback(server.stop)
        stack.push_async_callback(self._supervisor.aclose)
        addr = await server.start()
        logger.info("claudedock-core %s listening addr=%s", claudedock.__version__, addr)
        logger.info("daemon ready host=%s port=%s", self._config.host, self._config.port)

        loop = asyncio.get_running_loop()
        shutdown = asyncio.Event()
        loop.add_signal_handler(signal.SIGINT, shutdown.set)
        loop.add_signal_handler(signal.SIGTERM, shutdown.set)

        self.phase = "serving"
        await shutdown.wait()

        self.phase = "draining"
        logger.info("shutting down")
        loop.remove_signal_handler(signal.SIGINT)
        loop.remove_signal_handler(signal.SIGTERM)


# 同步入口：启动 CoreApp 事件循环
def run() -> None:
    asyncio.run(CoreApp().run())
