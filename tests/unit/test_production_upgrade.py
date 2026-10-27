from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from claudedock.core.bus.events import RunStartedEvent
from claudedock.core.config import ClaudeDockConfig
from claudedock.core.context import ExecutionContext
from claudedock.core.events.bus import EventBus
from claudedock.core.events.writer import EventWriter
from claudedock.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from claudedock.core.loop import AgentLoop
from claudedock.core.memory.model import MemoryEntry
from claudedock.core.memory.sqlite_store import MemorySqliteStore
from claudedock.core.permissions.manager import PermissionManager
from claudedock.core.rag.index import Bm25Index
from claudedock.core.rag.store import RagStore
from claudedock.core.runner import AgentRunner
from claudedock.core.security.redaction import redact
from claudedock.core.session.model import Session
from claudedock.core.session.store import SessionStore
from claudedock.core.storage import atomic_write
from claudedock.core.supervisor import RunSupervisor
from claudedock.core.task.manager import TaskManager
from claudedock.core.tools.builtin.bash import BashTool
from claudedock.core.tools.paths import resolve_inside
from claudedock.core.tools.registry import ToolRegistry
from claudedock.core.trace.record import TraceRecord
from claudedock.core.trace.writer import TraceWriter
from claudedock.core.transport.ipc_broadcaster import IpcEventBroadcaster
from claudedock.core.transport.socket_client import SocketClient
from claudedock.core.transport.socket_server import SocketServer, _writer_var


# 创建一个只返回固定响应的模型，完全禁止外部 API 依赖
class Provider:
    # 保存模型响应序列
    def __init__(self, responses):
        self.responses = iter(responses)

    # 返回下一条确定响应
    async def chat(self, **kwargs):
        return next(self.responses)


# 发送带可选认证凭证的原始帧，验证真实传输边界
async def rpc(reader, writer, token=None):
    params = {} if token is None else {"_auth_token": token}
    writer.write(
        json.dumps({"jsonrpc": "2.0", "id": "1", "method": "private", "params": params}).encode()
        + b"\n"
    )
    await writer.drain()
    return json.loads(await asyncio.wait_for(reader.readline(), timeout=1))


# 功能：未认证和错误密钥请求不能进入处理器，正确密钥不暴露给处理器
# 设计：真实 TCP 测试连续三种请求，直接检查调用次数与清洗后的参数
async def test_authenticated_dispatch():
    handler = AsyncMock(return_value={"ok": True})
    server = SocketServer("127.0.0.1", 0, auth_token="test-key")
    server.register("private", handler)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        assert (await rpc(reader, writer))["error"]["code"] == -32001
        assert (await rpc(reader, writer, "wrong"))["error"]["code"] == -32001
        handler.assert_not_called()
        assert (await rpc(reader, writer, "test-key"))["result"]["ok"]
        handler.assert_awaited_once_with({})
    finally:
        writer.close()
        await writer.wait_closed()
        await server.stop()


# 功能：客户端断线取消尚未完成的请求且释放注册表
# 设计：处理器在事件上等待，使用取消事件确认真实生命周期而不依赖固定 sleep
async def test_disconnect_cancels_request():
    entered, cancelled = asyncio.Event(), asyncio.Event()

    # 模拟长请求并记录取消传播
    async def waiting(params):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    server = SocketServer("127.0.0.1", 0, auth_token="key")
    server.register("private", waiting)
    await server.start()
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", server._server.sockets[0].getsockname()[1]
    )
    request = asyncio.create_task(rpc(reader, writer, "key"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(cancelled.wait(), 1)
        await server.stop()
        assert not server._requests
        assert not server._connections
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        await server.stop()


# 功能：重复一千次短期订阅后监听者数量保持稳定，事件只写到对应 Run
# 设计：用同一长驻总线模拟长期运行，同时发布外部 Run 事件检查并发隔离
async def test_thousand_event_writer_lifecycles(tmp_path):
    bus = EventBus()
    for i in range(1000):
        async with EventWriter(tmp_path / "events.jsonl", "mine") as writer:
            writer.subscribe(bus)
            await bus.publish(RunStartedEvent(run_id="other", goal="private", ts="t"))
            assert bus.subscriber_count == 1
        assert bus.subscriber_count == 0
    assert (tmp_path / "events.jsonl").read_text() == ""


# 功能：慢消费者不能阻塞其他消费者，关闭回收全部发送任务
# 设计：一个 drain 永久等待，一个正常 drain，用事件确认正常连接独立完成
async def test_slow_consumer_isolation():
    blocked, delivered = asyncio.Event(), asyncio.Event()
    slow, fast = MagicMock(), MagicMock()
    slow.drain = AsyncMock(side_effect=blocked.wait)

    # 标记健康连接完成一次发送
    async def drain():
        delivered.set()

    fast.drain = AsyncMock(side_effect=drain)
    broadcaster = IpcEventBroadcaster()
    broadcaster.subscribe(slow, ["run.*"])
    broadcaster.subscribe(fast, ["run.*"])
    try:
        await asyncio.wait_for(
            broadcaster.handle(RunStartedEvent(run_id="r", goal="g", ts="t")), 0.1
        )
        await asyncio.wait_for(delivered.wait(), 1)
        assert fast.write.call_count == 1
    finally:
        await broadcaster.aclose()
    assert not broadcaster._queues
    assert not broadcaster._subscriptions


# 功能：审批响应只能由发起连接提供，取消之后不残留 Future
# 设计：通过连接 ContextVar 模拟两个身份，错误身份不能使执行继续
async def test_approval_owner_and_cancellation():
    manager = PermissionManager()
    ready = asyncio.Event()
    owner, other = object(), object()

    # 审批事件发出后通知测试驱动
    async def emitted(event):
        ready.set()

    token = _writer_var.set(owner)
    try:
        task = asyncio.create_task(
            manager.check_and_wait("id", "bash", {"command": "echo"}, "s", emitted)
        )
    finally:
        _writer_var.reset(token)
    await ready.wait()
    with pytest.raises(PermissionError):
        manager.respond("id", "allow_once", owner=other)
    assert not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert manager._pending == {}


# 功能：永久允许只缓存同一会话的精确参数，参数或会话改变重新审批
# 设计：审批回调直接完成 Future，避免超时等待并统计三种请求的审批次数
async def test_exact_approval_scope():
    manager = PermissionManager()
    seen = []

    # 首次允许、之后拒绝以证明新请求经过审批
    async def emitted(event):
        seen.append(event)
        manager.respond(event["tool_use_id"], "always_allow" if len(seen) == 1 else "deny_once")

    assert (await manager.check_and_wait("1", "bash", {"command": "echo a"}, "s", emitted))[0]
    assert (await manager.check_and_wait("2", "bash", {"command": "echo a"}, "s", emitted))[0]
    assert not (await manager.check_and_wait("3", "bash", {"command": "echo b"}, "s", emitted))[0]
    assert not (await manager.check_and_wait("4", "bash", {"command": "echo a"}, "other", emitted))[
        0
    ]
    assert len(seen) == 3


# 功能：原子替换失败时保留旧文件并清理临时文件
# 设计：在提交窗口注入磁盘错误，断言旧内容仍完整，覆盖半写恢复风险
def test_atomic_write_failure_preserves_original(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    atomic_write(path, "old")

    # 模拟原子提交时磁盘故障
    def fail(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        atomic_write(path, "new")
    assert path.read_text() == "old"
    assert list(tmp_path.iterdir()) == [path]


# 功能：压缩提交失败保留原 thread，恢复备份不能造成原文件消失
# 设计：只让投影版本指针提交失败，真实写版本文件并检查审计历史保留
def test_compaction_commit_failure_keeps_thread(tmp_path, monkeypatch):
    store = SessionStore(tmp_path)
    store.append_message("sess", "user", "original")
    original = os.replace

    # 在 thread 提交窗口注入故障，允许恢复快照先落盘
    def fail(source, target):
        if Path(target).name == "context_projection.json":
            raise OSError("injected")
        return original(source, target)

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        store.write_compacted("sess", [{"role": "user", "content": "summary"}])
    assert store.read_messages("sess")[0]["content"] == "original"


# 功能：自动压缩后的完整新上下文落盘，重启后可恢复摘要与后续结果
# 设计：模拟压缩将旧历史缩短到两条消息，直接复现旧 prefill_len 截取错误
async def test_runner_persists_compacted_history(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "sessions")
    session = Session(
        id="sess",
        mode="chat",
        status="active",
        title="",
        created_at="t",
        updated_at="t",
        run_ids=[],
    )
    for i in range(8):
        store.append_message("sess", "user" if i % 2 == 0 else "assistant", str(i))

    # 模拟循环压缩历史并完成任务
    async def compact_run(self, context):
        context.compaction_version += 1
        context.messages = [
            {"role": "user", "content": "summary"},
            {"role": "assistant", "content": "continued"},
        ]
        context.mark_success()

    monkeypatch.setattr(AgentLoop, "run", compact_run)
    runner = AgentRunner(ClaudeDockConfig(), provider=Provider([]))
    await runner.run_and_capture("goal", session=session, store=store)
    assert SessionStore(tmp_path / "sessions").read_messages("sess") == [
        {"role": "user", "content": "summary"},
        {"role": "assistant", "content": "continued"},
    ]


# 功能：SQLite 整合批次任一步失败时全部回滚
# 设计：同一事务先修改条目再抛错，重开数据库验证磁盘上未留下部分更新
def test_memory_transaction_rollback(tmp_path):
    path = tmp_path / "memory.db"
    store = MemorySqliteStore(path)
    entry = MemoryEntry(id="m", content="old", kind="fact")
    store.upsert(entry)
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.set_status("m", "expired", "now")
            store.upsert(MemoryEntry(id="new", content="new", kind="fact"))
            raise RuntimeError("fault")
    store.close()
    recovered = MemorySqliteStore(path)
    assert recovered.get("m").status == "active"
    assert recovered.get("new") is None
    recovered.close()


# 功能：依赖环与未知依赖在修改文件前被拒绝
# 设计：两节点反向边形成最小环，拒绝后重新读取确认旧图未变化
def test_task_dag_rejects_cycles(tmp_path):
    manager = TaskManager(tmp_path)
    manager.create("one")
    manager.create("two", blocked_by=[1])
    with pytest.raises(ValueError, match="cyclic"):
        manager.update(1, add_blocked_by=[2])
    with pytest.raises(ValueError, match="not found"):
        manager.update(1, add_blocked_by=[999])
    assert manager.get(1).blocked_by == []


# 功能：跨文件更新中断后从已提交日志恢复完整状态
# 设计：第二个投影写入时失败，重新创建管理器触发日志重放
def test_task_transaction_recovers_interrupted_projection(tmp_path, monkeypatch):
    manager = TaskManager(tmp_path)
    manager.create("one")
    manager.create("two", blocked_by=[1])
    save = manager._save
    count = 0

    # 在批次第二次文件写入时模拟进程故障
    def fail(task):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("projection failure")
        save(task)

    monkeypatch.setattr(manager, "_save", fail)
    with pytest.raises(OSError):
        manager.update(1, status="completed")
    recovered = TaskManager(tmp_path)
    assert recovered.get(1).status == "completed"
    assert recovered.get(2).blocked_by == []
    assert not (tmp_path / "transaction.json").exists()


# 功能：RAG 重建失败保持旧索引、旧 chunk 与元数据一致
# 设计：初版真实 BM25 建索引，新版构建强制失败，再查询旧关键字检查回退
async def test_rag_failed_rebuild_preserves_previous(tmp_path, monkeypatch):
    source = tmp_path / "doc.md"
    source.write_text("alpha original")
    store = RagStore()
    await store.load_dir(tmp_path)
    source.write_text("beta changed")

    # 注入索引构建失败
    async def fail(*args, **kwargs):
        raise RuntimeError("build failure")

    monkeypatch.setattr(Bm25Index, "build", fail)
    with pytest.raises(RuntimeError):
        await store.reload()
    hits = await store.search("alpha")
    assert hits and "original" in hits[0].text
    assert not await store.search("beta")


# 功能：文本截断不能被标记为成功
# 设计：模型只返回 max_tokens 文本，检查对外状态与明确原因
async def test_truncated_response_is_failure():
    context = ExecutionContext("r", "goal", 10)
    loop = AgentLoop(
        Provider([LlmResponse(stop_reason="max_tokens", text="partial")]),
        ToolRegistry(),
        EventBus(),
    )
    await loop.run(context)
    assert context.status == "failed"
    assert context.reason == "truncated"


# 功能：超过 Token 预算时停止且不执行返回的工具动作
# 设计：超额模型响应含工具调用，确保预算检查先于工具执行
async def test_token_budget_stops_before_tools():
    context = ExecutionContext("r", "goal", 10)
    response = LlmResponse(
        stop_reason="tool_use",
        tool_calls=[ToolCallBlock("t", "unknown", {})],
        usage=UsageStats(100, 100),
    )
    loop = AgentLoop(Provider([response]), ToolRegistry(), EventBus(), max_tokens=1)
    await loop.run(context)
    assert context.reason == "token_budget_exhausted"
    assert len(context.messages) == 1


# 功能：没有操作系统沙箱时默认拒绝 Shell 执行
# 设计：隐藏 bwrap 可执行文件，用真实调用验证没有退回宿主 shell
async def test_shell_fails_closed_without_sandbox(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    result = await BashTool().invoke({"command": "echo should-not-run"})
    assert result.is_error
    assert result.error_type == "permission_denied"
    assert "should-not-run" not in result.content


# 功能：日志与 Trace 脱敏保留 Token 数量和关联键
# 设计：混合嵌套密钥、Bearer 文本和数字指标，防止脱敏误删观测数据
def test_redaction_preserves_usage_and_removes_secrets():
    value = redact(
        {
            "api_key": "private",
            "input_tokens": 123,
            "run_id": "r",
            "nested": [{"_auth_token": "secret"}],
            "msg": "Bearer abcdefgh",
        }
    )
    assert value["input_tokens"] == 123 and value["run_id"] == "r"
    assert "private" not in str(value) and "secret" not in str(value)
    assert "abcdefgh" not in str(value)


# 功能：Trace 队列有固定上限，超量写入不会增长内存
# 设计：暂停消费者后写入超过容量的记录，检查丢弃计数与队列大小
def test_trace_queue_is_bounded(tmp_path):
    trace = TraceWriter(tmp_path / "trace.jsonl")
    record = TraceRecord(ts="t", direction="CORE", layer="event", kind="event", data={})
    for _ in range(3000):
        trace.emit(record)
    assert trace._queue.qsize() == 2048
    assert trace.dropped == 952


# 功能：敏感路径、父目录穿越及符号链接越界均被拒绝
# 设计：在允许根目录中创建恶意链接，验证解析后的实际资源范围
def test_sensitive_and_symlink_paths(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "escape").symlink_to(tmp_path)
    for path in (".env", ".git/config", "escape/secret", "../secret"):
        assert resolve_inside(root, path).path is None


# 功能：后台作业限流、同键去重和关闭取消统一生效
# 设计：任务保持等待状态，精确验证容量拒绝与取消后无托管任务
async def test_supervisor_bounds_and_shutdown():
    supervisor = RunSupervisor(max_runs=1, max_jobs=1)
    gate = asyncio.Event()
    task = supervisor.spawn(gate.wait(), key="job")
    assert supervisor.spawn(gate.wait(), key="job") is task
    with pytest.raises(RuntimeError, match="capacity"):
        supervisor.spawn(gate.wait())
    await supervisor.aclose()
    assert task.cancelled()
    assert not supervisor._tasks


# 功能：客户端请求超时后清理 Future 并可继续使用连接
# 设计：不启动服务端读取，模拟请求失联，只验证有限等待与 pending 回收
async def test_client_timeout_cleans_pending():
    client = SocketClient("127.0.0.1", 1, token="key", request_timeout=0.01)
    writer = MagicMock()
    writer.drain = AsyncMock()
    client._writer = writer
    with pytest.raises(TimeoutError):
        await client.send_command("private", {})
    assert client._pending == {}
