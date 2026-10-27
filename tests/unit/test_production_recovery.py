from __future__ import annotations

import asyncio
import json
import signal
from unittest.mock import AsyncMock, MagicMock

import pytest

from claudedock.core.app import CoreApp
from claudedock.core.bus.events import RunStartedEvent
from claudedock.core.config import ClaudeDockConfig
from claudedock.core.context import ExecutionBudget, ExecutionContext
from claudedock.core.events.bus import EventBus
from claudedock.core.journal import RunJournal, current_journal
from claudedock.core.llm.governed import GovernedProvider
from claudedock.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from claudedock.core.loop import AgentLoop
from claudedock.core.mcp.client import McpClient, McpServerUnavailableError, McpToolError
from claudedock.core.runtime_config import RuntimeConfig, apply_runtime
from claudedock.core.session.manager import SessionManager
from claudedock.core.session.model import Session
from claudedock.core.session.store import SessionStore
from claudedock.core.subagent.registry import BackgroundTaskRegistry
from claudedock.core.supervisor import RunSupervisor
from claudedock.core.tools.base import BaseTool, ToolResult
from claudedock.core.tools.paths import read_inside, write_inside
from claudedock.core.tools.registry import ToolRegistry
from claudedock.core.trace.record import TraceRecord
from claudedock.core.trace.writer import TraceWriter
from claudedock.core.transport.auth import read_token, server_token, token_path
from claudedock.core.transport.ipc_broadcaster import IpcEventBroadcaster
from claudedock.core.transport.socket_client import SocketClient
from tests.security_helpers import invoke_tool


# 创建包含运行身份的可恢复会话
def session():
    return Session(
        id="sess",
        mode="chat",
        status="active",
        title="goal",
        created_at="t",
        updated_at="t",
        run_ids=["run"],
    )


# 功能：守护进程完成初始化、响应带认证 ping、关闭后无残留订阅或任务
# 设计：真实启动 CoreApp 与 TCP 服务器，只替换信号触发，避免真实 API 和操作系统信号干扰
async def test_core_start_ping_and_shutdown(tmp_path, monkeypatch):
    config = ClaudeDockConfig(port=0, sessions_dir=str(tmp_path / "sessions"))
    config.logging.file = ""
    config.trace.file = str(tmp_path / "trace.jsonl")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDEDOCK_IPC_TOKEN", raising=False)
    monkeypatch.setattr("claudedock.core.app.get_config", lambda: config)
    loop = asyncio.get_running_loop()
    signals = {}
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda name, callback: signals.setdefault(name, callback)
    )
    monkeypatch.setattr(loop, "remove_signal_handler", lambda name: True)
    app = CoreApp()
    task = asyncio.create_task(app.run())
    client = None
    reader_task = None
    try:
        async with asyncio.timeout(3):
            while app.phase != "serving":
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        port = app._server._server.sockets[0].getsockname()[1]
        client = SocketClient("127.0.0.1", port, token=read_token(0), request_timeout=1)
        await client.connect()
        reader_task = asyncio.create_task(client.run_event_loop())
        response = await client.send_command("core.ping", {})
        assert response["server_version"] == "0.0.1"
        signals[signal.SIGTERM]()
        await asyncio.wait_for(task, 3)
        assert app.phase == "closed"
        assert app._bus.subscriber_count == 0
        assert not app._supervisor._tasks
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if client:
            await client.close()
        if reader_task:
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)


# 功能：启动中途发生错误时已创建的 Trace 与广播器仍逆序关闭
# 设计：在会话恢复阶段注入异常，检查真正后台任务被回收而非仅断言 close 被调用
async def test_core_start_failure_cleans_resources(tmp_path, monkeypatch):
    config = ClaudeDockConfig(port=0, sessions_dir=str(tmp_path / "sessions"))
    config.logging.file = ""
    config.trace.file = str(tmp_path / "trace.jsonl")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDEDOCK_IPC_TOKEN", raising=False)
    monkeypatch.setattr("claudedock.core.app.get_config", lambda: config)
    monkeypatch.setattr(
        SessionManager, "restore", AsyncMock(side_effect=OSError("startup failure"))
    )
    app = CoreApp()
    with pytest.raises(OSError):
        await app.run()
    assert app.phase == "closed"
    assert app._trace._task.done()
    assert app._bus.subscriber_count == 0


# 功能：用户消息与 Run 身份投影中断时写前日志恢复为同一事务
# 设计：在 meta 投影写入前失败，重开 Store 后检查消息 run_id 与 meta.run_ids 一致
def test_session_transaction_recovers(tmp_path, monkeypatch):
    store = SessionStore(tmp_path)
    original = __import__("claudedock.core.session.store", fromlist=["atomic_write"]).atomic_write

    # 仅阻断元数据投影，不阻断事务日志
    def fail(path, content):
        if path.name == "meta.json":
            raise OSError("injected")
        original(path, content)

    monkeypatch.setattr("claudedock.core.session.store.atomic_write", fail)
    with pytest.raises(OSError):
        store.begin_run(session(), "question", "run")
    monkeypatch.setattr("claudedock.core.session.store.atomic_write", original)
    recovered = SessionStore(tmp_path)
    assert recovered.read_meta("sess").run_ids == ["run"]
    rows = json.loads((tmp_path / "sess/thread.jsonl").read_text())
    assert rows["run_id"] == "run" and rows["content"] == "question"
    assert not (tmp_path / "sess/session.transaction.json").exists()


# 功能：重启后从运行检查点恢复压缩历史，并将未完成 Run 标记为 interrupted
# 设计：磁盘 thread 故意写旧历史，检查点写两条新摘要，恢复后验证两层状态同时更新
async def test_checkpoint_restore_after_process_interruption(tmp_path):
    store = SessionStore(tmp_path)
    store.write_meta(session())
    store.append_message("sess", "user", "old history")
    path = store.runs_dir("sess") / "run/checkpoint.json"
    journal = RunJournal(path, "run", "original goal")
    messages = [
        {"role": "user", "content": "durable summary"},
        {"role": "assistant", "content": "continue"},
    ]
    journal.checkpoint(messages)
    manager = SessionManager(store, lambda: None, EventBus())
    result = await manager.restore()
    assert result.interrupted == 1
    assert store.read_messages("sess") == messages
    assert json.loads(path.read_text())["state"] == "interrupted"


# 功能：同一操作号重试只返回已提交结果，未知结果不再次执行副作用
# 设计：用计数工具真实调用一次，再重试完成及执行中两种日志状态
async def test_operation_id_prevents_duplicate_side_effect(tmp_path):
    calls = []

    class SideEffect(BaseTool):
        name = "effect"
        description = "effect"
        input_schema = {"type": "object"}

        # 模拟不可重复的副作用
        async def invoke(self, params):
            calls.append(params)
            return ToolResult("committed")

    registry = ToolRegistry()
    registry.register(SideEffect())
    journal = RunJournal(tmp_path / "checkpoint.json", "r", "goal")
    token = current_journal.set(journal)
    try:
        call = ToolCallBlock("id", "effect", {})
        assert (await invoke_tool(registry, call, EventBus(), "r")).content == "committed"
        assert (await invoke_tool(registry, call, EventBus(), "r")).content == "committed"
        journal.update("id", "executing")
        result = await invoke_tool(registry, call, EventBus(), "r")
        assert result.error_type == "unknown_outcome"
        assert len(calls) == 1
    finally:
        current_journal.reset(token)


# 功能：父子 Agent 共用模型调用与 Token 总预算而非各自重置
# 设计：两个 Loop 顺序使用同一预算与真实治理包装器，第二次调用在发出请求前被阻断
async def test_shared_model_budget():
    inner = MagicMock()
    inner.chat = AsyncMock(
        return_value=LlmResponse(stop_reason="end_turn", usage=UsageStats(20, 5))
    )
    provider = GovernedProvider(inner, asyncio.Semaphore(1))
    budget = ExecutionBudget(max_model_calls=1)
    parent = ExecutionContext("parent", "goal", 10, budget=budget)
    child = ExecutionContext("child", "goal", 10, budget=budget)
    await AgentLoop(provider, ToolRegistry(), EventBus()).run(parent)
    await AgentLoop(provider, ToolRegistry(), EventBus()).run(child)
    assert parent.status == "success"
    assert child.reason == "model_call_budget_exhausted"
    assert budget.model_calls == 1 and budget.tokens == 25
    assert inner.chat.await_count == 1


# 功能：模型价格显式配置后费用可累计且超预算不执行模型返回的工具
# 设计：价格用测试常数而非真实价目表，精确检查合并费用与停止原因
async def test_explicit_cost_budget():
    inner = MagicMock()
    inner.chat = AsyncMock(
        return_value=LlmResponse(stop_reason="tool_use", usage=UsageStats(1_000_000, 1_000_000))
    )
    provider = GovernedProvider(
        inner, asyncio.Semaphore(1), {"input": 1, "output": 2, "cache_read": 0, "cache_create": 0}
    )
    context = ExecutionContext(
        "r", "goal", 10, budget=ExecutionBudget(max_tokens=3_000_000, max_cost_usd=2)
    )
    await AgentLoop(provider, ToolRegistry(), EventBus()).run(context)
    assert context.reason == "cost_budget_exhausted"
    assert context.budget.estimated_cost == 3


# 功能：启用费用预算但未知模型价格时拒绝发出付费请求
# 设计：模型 mock 不应被调用，证明不会把未知价格默认为零
async def test_unknown_price_fails_closed():
    inner = MagicMock()
    inner.chat = AsyncMock()
    provider = GovernedProvider(inner, asyncio.Semaphore(1))
    context = ExecutionContext("r", "goal", 10, budget=ExecutionBudget(max_cost_usd=1))
    await AgentLoop(provider, ToolRegistry(), EventBus()).run(context)
    assert context.reason == "cost_pricing_unavailable"
    inner.chat.assert_not_called()


# 功能：父运行取消能回收所有后台后代，而不影响其他任务树
# 设计：构造父、孙、无关三节点，检查取消集合与保留任务
async def test_parent_cancellation_tree():
    registry = BackgroundTaskRegistry()
    gate = asyncio.Event()
    child = asyncio.create_task(gate.wait())
    grandchild = asyncio.create_task(gate.wait())
    unrelated = asyncio.create_task(gate.wait())
    registry.register("c", child, ExecutionContext("c", "g", 1, parent_run_id="root"))
    registry.register("g", grandchild, ExecutionContext("g", "g", 1, parent_run_id="c"))
    registry.register("u", unrelated, ExecutionContext("u", "g", 1, parent_run_id="other"))
    try:
        await registry.cancel_children("root")
        assert child.cancelled() and grandchild.cancelled()
        assert not unrelated.done()
    finally:
        await registry.aclose()


# 功能：运行状态经历 queued/running/cancelling/interrupted，取消后没有孤儿请求
# 设计：用开始事件同步取消操作，检查可查询状态而非仅检查 Task.cancel 返回值
async def test_supervisor_state_and_cancel():
    supervisor = RunSupervisor()
    entered = asyncio.Event()

    # 模拟可取消运行
    async def work():
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(supervisor.run(work(), run_id="r"))
    await entered.wait()
    assert supervisor.state("r") == "running"
    assert supervisor.cancel("r")
    await asyncio.gather(task, return_exceptions=True)
    assert supervisor.state("r") == "interrupted"
    await supervisor.aclose()


# 功能：本地凭证以 0600 创建且重启复用，过宽权限与符号链接均拒绝
# 设计：临时 HOME 隔离真实凭证，真实文件权限操作覆盖 Linux 授权边界
def test_local_token_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDEDOCK_IPC_TOKEN", raising=False)
    token = server_token(7437)
    assert len(token) >= 32 and read_token(7437) == token
    assert server_token(7437) == token
    path = token_path(7437)
    assert path.stat().st_mode & 0o777 == 0o600
    path.chmod(0o644)
    with pytest.raises(PermissionError):
        read_token(7437)
    path.unlink()
    path.symlink_to(tmp_path / "missing")
    with pytest.raises(OSError):
        read_token(7437)


# 功能：MCP 初始化确认协议版本与能力，未知版本或无效身份拒绝连接
# 设计：模拟 JSON-RPC initialize 响应，仅在合法协商完成后发送 initialized
@pytest.mark.parametrize("version", ["2024-11-05", "2025-06-18", "unsupported"])
async def test_mcp_protocol_negotiation(version):
    client = McpClient()
    client._call = AsyncMock(
        return_value={
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "test", "version": "1"},
        }
    )
    client._notify = AsyncMock()
    if version == "unsupported":
        with pytest.raises(McpServerUnavailableError):
            await client._initialize()
        client._notify.assert_not_called()
    else:
        await client._initialize()
        client._notify.assert_awaited_once()
        assert client.protocol_version == version


# 功能：MCP 工具 isError 与非法内容结构不能被包装成成功
# 设计：三种不同应用层错误输入，应都返回明确协议异常
@pytest.mark.parametrize("response", [{"isError": True}, {"content": "invalid"}, {"content": [1]}])
async def test_mcp_invalid_tool_response(response):
    client = McpClient()
    client._call = AsyncMock(return_value=response)
    with pytest.raises(McpToolError):
        await client.call_tool("tool", {})


# 功能：外部 Schema 的参数验证在执行前拒绝类型错误与额外属性
# 设计：仅有 JSON Schema 而无 Pydantic 模型的工具模拟真实 MCP 边界
async def test_external_schema_validation_precedes_execution():
    invoked = []

    class External(BaseTool):
        name = "external"
        description = "external"
        input_schema = {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
            "additionalProperties": False,
        }

        # 记录真正进入执行的输入
        async def invoke(self, params):
            invoked.append(params)
            return ToolResult("ok")

    registry = ToolRegistry()
    registry.register(External())
    for params in ({}, {"count": "one"}, {"count": 1, "extra": 2}):
        result = await invoke_tool(
            registry, ToolCallBlock("id", "external", params), EventBus(), "r"
        )
        assert result.error_type == "schema_error"
    assert not invoked


# 功能：不可信 Schema 不能触发外部引用下载或宿主正则运算
# 设计：注册时就拒绝资源引用和正则，避免依赖工具执行时的超时
@pytest.mark.parametrize(
    "schema",
    [{"$ref": "https://untrusted.invalid/schema"}, {"type": "string", "pattern": "(a+)+$"}],
)
def test_untrusted_schema_resources_rejected(schema):
    class External(BaseTool):
        name = "external"
        description = "external"
        input_schema = schema

        # 本测试不允许进入执行阶段
        async def invoke(self, params):
            raise AssertionError("not invoked")

    with pytest.raises(ValueError):
        ToolRegistry().register(External())


# 功能：实时发送在历史回放结束之前暂停，队列字节预算超过后断开慢客户端
# 设计：用暂停发送模拟长回放，在不依赖网络缓冲大小的情况下验证硬边界
async def test_replay_gate_and_byte_budget():
    writer = MagicMock()
    writer.drain = AsyncMock()
    broadcaster = IpcEventBroadcaster()
    broadcaster.subscribe(writer, ["run.*"], paused=True)
    event = RunStartedEvent(run_id="r", goal="g", ts="t")
    await broadcaster.handle(event)
    await asyncio.sleep(0)
    writer.write.assert_not_called()
    broadcaster.resume(writer)
    await asyncio.wait_for(broadcaster._queues[writer].join(), 1)
    assert writer.write.call_count == 1
    broadcaster.unsubscribe(writer)
    broadcaster.subscribe(writer, ["run.*"], paused=True)
    await broadcaster.handle(RunStartedEvent(run_id="r", goal="x" * (3 * 1024 * 1024), ts="t"))
    writer.close.assert_called()
    await broadcaster.aclose()


# 功能：会话订阅只接收自己及子 Agent 的运行事件
# 设计：两个会话并行发布，检查实际发送的 run_id 集合
async def test_session_event_isolation():
    writer = MagicMock()
    writer.drain = AsyncMock()
    broadcaster = IpcEventBroadcaster()
    broadcaster.bind_run("mine", "s1")
    broadcaster.bind_run("other", "s2")
    broadcaster.subscribe(writer, ["run.*"], scope="session:s1")
    await broadcaster.handle(RunStartedEvent(run_id="mine", goal="g", ts="t"))
    await broadcaster.handle(RunStartedEvent(run_id="other", goal="g", ts="t"))
    await asyncio.wait_for(broadcaster._queues[writer].join(), 1)
    assert writer.write.call_count == 1
    assert json.loads(writer.write.call_args[0][0])["event"]["run_id"] == "mine"
    await broadcaster.aclose()


# 功能：回放游标丢弃重复事件但保留新的顺序事件
# 设计：真实客户端分发器接收重叠序列，确认不会重复显示或重复审批
async def test_client_event_cursor_deduplication():
    client = SocketClient("localhost", 1, token="key")
    events = []

    # 收集交给前端的事件
    async def collect(event):
        events.append(event)

    client.on_event(collect)
    for sequence in (1, 1, 2, 1, 3):
        await client._dispatch(
            json.dumps(
                {
                    "kind": "event",
                    "sequence": sequence,
                    "event": {"type": "run.started", "run_id": "r"},
                }
            ).encode()
        )
    assert len(events) == 3
    assert client._cursors["r"] == 3


# 功能：父目录在路径校验后变成符号链接时读写仍不能越界
# 设计：先保存已验证路径，再置换目录，直接复现 TOCTOU 竞态窗口
def test_descriptor_access_rejects_parent_swap(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    directory = root / "dir"
    directory.mkdir()
    target = directory / "secret"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("private")
    directory.rmdir()
    directory.symlink_to(outside)
    with pytest.raises(OSError):
        read_inside(root, target, 10)
    with pytest.raises(OSError):
        write_inside(root, target, "overwritten")
    assert (outside / "secret").read_text() == "private"


# 功能：Trace 默认省略内容与审批参数，并按上限滚动保留文件
# 设计：向小容量 Writer 连续写正文记录，检查正文不可见且备份数量有界
async def test_trace_privacy_and_rotation(tmp_path):
    trace = TraceWriter(tmp_path / "trace.jsonl", max_bytes=300, backups=2)
    await trace.start()
    for i in range(20):
        trace.emit(
            TraceRecord(
                ts="t",
                direction="CORE",
                layer="event",
                kind="event",
                data={"goal": "private goal", "params": {"password": "secret"}, "step": i},
            )
        )
    await trace.stop()
    files = list(tmp_path.glob("trace.jsonl*"))
    assert 1 <= len(files) <= 3
    assert all(
        "private goal" not in file.read_text() and "secret" not in file.read_text()
        for file in files
    )


# 功能：配置拒绝 NaN、无限值、布尔和分数并保留合法旧配置
# 设计：对运行限额的错误类别参数化，避免只覆盖一个具体配置键
@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, 1.5])
def test_runtime_rejects_invalid_limits(value):
    with pytest.raises(SystemExit):
        apply_runtime(RuntimeConfig(), {"max_runs": value})


# 功能：重启不允许已完成运行的旧检查点覆盖之后的手动压缩结果
# 设计：真实文件存储写入两份历史，再恢复会话并确认较新历史被保留
async def test_completed_checkpoint_does_not_overwrite_newer_history(tmp_path):
    store = SessionStore(tmp_path)
    item = session()
    store.write_meta(item)
    journal = RunJournal(store.runs_dir(item.id) / "run" / "checkpoint.json", "run", "goal")
    journal.data.update(state="completed", messages=[{"role": "user", "content": "old"}])
    journal.save()
    store.write_compacted(item.id, [{"role": "user", "content": "new summary"}])
    manager = SessionManager(store, MagicMock(), EventBus())
    await manager.restore()
    assert store.read_messages(item.id)[0]["content"] == "new summary"


# 功能：TLS IPC 验证受信证书并拒绝未受信证书
# 设计：生成仅限回环地址的临时证书，在真实加密连接上执行认证请求
async def test_tls_authenticated_ipc_and_untrusted_certificate(tmp_path, monkeypatch):
    import shutil
    import ssl
    import subprocess

    from claudedock.core.transport.socket_server import SocketServer

    if not shutil.which("openssl"):
        pytest.skip("openssl required for certificate fixture")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    server = SocketServer("127.0.0.1", 0, auth_token="t" * 32, ssl_context=context)
    server.register("ping", AsyncMock(return_value={"ok": True}))
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    monkeypatch.setenv("CLAUDEDOCK_TLS_CA", str(cert))
    client = SocketClient("127.0.0.1", port, token="t" * 32, request_timeout=2)
    reader = None
    try:
        await client.connect()
        reader = asyncio.create_task(client.run_event_loop())
        assert await client.send_command("ping", {}) == {"ok": True}
        monkeypatch.delenv("CLAUDEDOCK_TLS_CA")
        monkeypatch.setenv("CLAUDEDOCK_TLS_ENABLE", "1")
        with pytest.raises(ssl.SSLCertVerificationError):
            await SocketClient("127.0.0.1", port).connect()
    finally:
        await client.close()
        if reader is not None:
            await asyncio.gather(reader, return_exceptions=True)
        await server.stop()


# 功能：记忆索引刷新保留正在被读取的旧版本，读者退出后才删除
# 设计：阻塞旧索引搜索并发布新索引，验证返回旧条目且回收恰好一次
async def test_memory_snapshot_retired_after_last_reader():
    from claudedock.core.memory.model import MemoryEntry
    from claudedock.core.memory.vector_store import MemoryVectorStore

    initial, old, new = MagicMock(), MagicMock(), MagicMock()
    initial.fork.return_value = old
    old.fork.return_value = new
    for index in (initial, old, new):
        index.build = AsyncMock()
        index.retire = AsyncMock()
    entered, release = asyncio.Event(), asyncio.Event()

    # 持有旧索引直到构建新版本完成
    async def delayed_search(*args):
        entered.set()
        await release.wait()
        return [(0, 1.0)]

    old.search = delayed_search
    store = MemoryVectorStore(initial, chunk_size=200, chunk_overlap=20)
    await store.build([MemoryEntry(id="old", content="old knowledge", kind="fact")])
    task = asyncio.create_task(store.search("knowledge", 1))
    await entered.wait()
    try:
        await store.build([MemoryEntry(id="new", content="new knowledge", kind="fact")])
        old.retire.assert_not_awaited()
    finally:
        release.set()
    assert (await task)[0][0].id == "old"
    old.retire.assert_awaited_once()
    assert not store._readers and not store._retired


# 功能：压缩摘要缺字段、字段为空或次序错误时不替换原历史
# 设计：模拟三种结构不完整的模型输出，验证压缩被拒绝
@pytest.mark.parametrize("invalid", ["missing", "empty", "reordered"])
async def test_compaction_rejects_invalid_section_integrity(tmp_path, invalid):
    from claudedock.core.compact.compactor import Compactor

    titles = [
        "Original Goal",
        "Completed Steps",
        "Key Constraints & Discoveries",
        "Current File State",
        "Remaining TODOs",
        "Critical Data",
    ]
    sections = [f"## {i}. {title}\nNone" for i, title in enumerate(titles, 1)]
    if invalid == "missing":
        sections.pop()
    elif invalid == "empty":
        sections[2] = "## 3. Key Constraints & Discoveries\n"
    else:
        sections[0], sections[1] = sections[1], sections[0]
    provider = MagicMock()
    provider.chat = AsyncMock(
        return_value=LlmResponse(text="\n".join(sections), stop_reason="end_turn")
    )
    context = ExecutionContext(run_id="summary", goal="preserve goal", max_steps=2)
    original = list(context.messages)
    result = await Compactor(EventBus(), tmp_path, "sess").compact(context, provider)
    assert result is None
    assert context.messages == original
