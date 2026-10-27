from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from claudedock.core.compact.microcompact import validate_messages
from claudedock.core.compact.tool_result_store import (
    ResultBudget,
    ToolResultStore,
    current_result_budget,
)
from claudedock.core.config import ToolExecutionConfig
from claudedock.core.context import ExecutionContext
from claudedock.core.events.bus import EventBus
from claudedock.core.journal import RunJournal, current_journal
from claudedock.core.llm.types import LlmResponse, ToolCallBlock
from claudedock.core.loop import AgentLoop
from claudedock.core.permissions.manager import PermissionManager
from claudedock.core.tools.base import ToolResult
from claudedock.core.tools.builtin.read_file import ReadFileTool
from claudedock.core.tools.concurrency import run_file_io
from claudedock.core.tools.invocation import invoke_tool
from claudedock.core.tools.registry import ToolRegistry
from claudedock.core.tools.scheduler import ToolExecutionScheduler
from tests.security_helpers import approving_manager
from tests.unit.test_tool_scheduler import ProbeTool, call


# 组装真实执行网关、安全门、审批器与共享执行槽
def scheduler_for(registry, bus, manager, slots=None, **options):
    # 所有测试调用均经过生产安全入口
    async def invoke(tc, execution_slots):
        return await invoke_tool(
            registry,
            tc,
            bus,
            "r",
            permission_manager=manager,
            session_id="s",
            execution_slots=execution_slots,
        )

    return ToolExecutionScheduler(
        registry,
        invoke,
        bus,
        slots or asyncio.Semaphore(4),
        ToolExecutionConfig(parallel_enabled=True, **options),
    )


# 功能：验证真实文件读取在线程中重叠且每次仍经过安全决策
# 设计：在线程内部用双向屏障证明两次 I/O 同时在途，同时验证敏感路径仍被拒绝
async def test_real_file_overlap_and_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("a", "b"):
        (tmp_path / name).write_text(name)
    tool = ReadFileTool(tmp_path)
    original = tool._invoke_sync
    barrier = threading.Barrier(2)

    # 若文件操作仍在事件循环串行执行，屏障将超时导致测试失败
    def sync(params):
        barrier.wait(timeout=2)
        return original(params)

    monkeypatch.setattr(tool, "_invoke_sync", sync)
    registry = ToolRegistry()
    registry.register(tool)
    bus = EventBus()
    events = []

    # 收集真实安全决策与批次事件
    async def record(event):
        events.append(event)

    bus.subscribe(record)
    manager = PermissionManager(project_root=tmp_path, rules=())
    scheduler = scheduler_for(registry, bus, manager)
    results = await scheduler.execute(
        [ToolCallBlock(name, "read_file", {"path": name}) for name in ("a", "b")], run_id="r"
    )
    assert [r.content for r in results] == ["a", "b"]
    denied = await scheduler.execute(
        [ToolCallBlock("denied", "read_file", {"path": ".env"})], run_id="r"
    )
    assert denied[0].error_type == "permission_denied"
    assert sum(e.type == "security.decision" for e in events) == 3
    finished = [e for e in events if e.type == "tool.batch_finished"]
    assert finished[0].peak_executing == 2
    assert not any("params" in e.model_dump() for e in events if e.type.startswith("tool.batch_"))


# 功能：验证人工审批等待不占执行槽且取消会清理全部审批 Future
# 设计：单槽下让两调用同时进入审批，再取消批次，直接检查管理器和槽容量
async def test_approvals_do_not_hold_slots(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(ProbeTool(tmp_path), trusted_concurrency=True)
    bus = EventBus()
    manager = PermissionManager(project_root=tmp_path, rules=(), timeout_s=0)
    two_requests = asyncio.Event()

    # 两个待审批调用同时存在才能说明审批没有占住唯一执行槽
    async def record(event):
        if event.type == "permission.requested" and len(manager._pending) == 2:
            two_requests.set()

    bus.subscribe(record)
    slots = asyncio.Semaphore(1)
    scheduler = scheduler_for(registry, bus, manager, slots)
    task = asyncio.create_task(scheduler.execute([call(0), call(1)], run_id="r"))
    await asyncio.wait_for(two_requests.wait(), 1)
    assert slots._value == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager._pending == {}
    assert slots._value == 1


# 功能：验证审批队列溢出有配对拒绝且不混用其他调用的批准
# 设计：只保留一个待审批请求，另一调用应立即失败，再单独批准原请求
async def test_approval_capacity_and_binding(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(ProbeTool(tmp_path), trusted_concurrency=True)
    bus = EventBus()
    manager = PermissionManager(project_root=tmp_path, rules=(), max_pending_approvals=1)
    rejected = asyncio.Event()
    requests = []

    # 保留一次性 token，并等待另一个调用明确拒绝
    async def record(event):
        if event.type == "permission.requested":
            requests.append(event)
        if event.type == "tool.call_failed" and event.error_class == "permission_denied":
            rejected.set()

    bus.subscribe(record)
    task = asyncio.create_task(
        scheduler_for(registry, bus, manager).execute([call(0), call(1)], run_id="r")
    )
    await asyncio.wait_for(rejected.wait(), 1)
    assert len(requests) == 1
    request = requests[0]
    manager.respond(request.tool_use_id, "allow_once", approval_token=request.approval_token)
    results = await task
    assert not results[0].is_error
    assert results[1].error_type == "permission_denied"
    assert manager._pending == {}


# 功能：验证多个 Run 共享全局槽且单运行上限独立生效
# 设计：工具内部统计真实执行峰值，对比全局上限一与每 Run 上限一
@pytest.mark.parametrize("global_limit,per_run,expected", [(1, 4, 1), (4, 1, 2)])
async def test_shared_limits(
    tmp_path: Path, global_limit: int, per_run: int, expected: int
) -> None:
    active, peak = 0, 0
    both = asyncio.Event()

    # 让两个独立 Run 的执行可重叠，峰值受全局或单运行较小限制约束
    async def invoke(params):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == expected:
            both.set()
        try:
            await asyncio.wait_for(both.wait(), 1)
            await asyncio.sleep(0.01)
            return ToolResult("ok")
        finally:
            active -= 1

    registry = ToolRegistry()
    tool = ProbeTool(tmp_path)
    tool.invoke = invoke
    registry.register(tool, trusted_concurrency=True)
    bus = EventBus()
    manager = approving_manager(bus)
    manager.project_root = tmp_path
    slots = asyncio.Semaphore(global_limit)
    schedulers = [
        scheduler_for(registry, bus, manager, slots, max_concurrency_per_run=per_run)
        for _ in range(2)
    ]
    results = await asyncio.gather(
        *[
            s.execute([call(i * 2), call(i * 2 + 1)], run_id=f"r{i}")
            for i, s in enumerate(schedulers)
        ]
    )
    assert all(not r.is_error for batch in results for r in batch)
    assert peak == expected and active == 0


# 功能：验证集中预算按输入顺序分配且恢复不重复已完成操作
# 设计：强制第二调用先完成，检查第一调用仍优先获得预算并核对持久化操作输出
async def test_collector_budget_journal_and_replay(tmp_path: Path) -> None:
    registry = ToolRegistry()
    tool = ProbeTool(tmp_path)
    second = asyncio.Event()
    executions = []

    # 逆序完成的大结果用于区分完成顺序与预算分配顺序
    async def invoke(params):
        path = params["path"]
        executions.append(path)
        if path == "a":
            await second.wait()
        else:
            second.set()
        return ToolResult(str(path) * 700)

    tool.invoke = invoke
    registry.register(tool, trusted_concurrency=True)
    bus = EventBus()
    manager = approving_manager(bus)
    manager.project_root = tmp_path
    store = ToolResultStore(tmp_path / "outputs", max_bytes=2000)
    journal = RunJournal(tmp_path / "journal.json", "r", "test")
    journal_token = current_journal.set(journal)
    budget_token = current_result_budget.set(ResultBudget(store, "r", 1000))
    scheduler = scheduler_for(registry, bus, manager)
    try:
        results = await scheduler.execute([call(0, "a"), call(1, "b")], run_id="r")
        assert results[0].content == "a" * 700
        assert "tool-result-ref:" in results[1].content
        assert sum(len(r.content.encode()) for r in results) <= 1000
        current_result_budget.set(None)
        replay = await scheduler.execute([call(0, "a"), call(1, "b")], run_id="r")
        assert replay == results
        assert executions == ["a", "b"]
        disk = json.loads(journal.path.read_text())
        assert all(op["state"] == "completed" for op in disk["operations"].values())
        assert disk["operations"]["1"]["output"] == results[1].content
    finally:
        current_journal.reset(journal_token)
        current_result_budget.reset(budget_token)


# 功能：验证存储失败不重试成功工具，取消与超时保留未决状态
# 设计：使用真实 Journal 与极小存储配额，分别覆盖副作用完成后和执行中断窗口
async def test_storage_failure_and_unknown_outcome(tmp_path: Path) -> None:
    registry = ToolRegistry()
    tool = ProbeTool(tmp_path)
    count = 0
    started = asyncio.Event()

    # 同一工具可模拟成功大输出或永不完成的外部操作
    async def invoke(params):
        nonlocal count
        count += 1
        if params.get("wait"):
            started.set()
            await asyncio.Event().wait()
        return ToolResult("done" * 100)

    tool.invoke = invoke
    registry.register(tool, trusted_concurrency=True)
    bus = EventBus()
    manager = approving_manager(bus)
    manager.project_root = tmp_path
    journal = RunJournal(tmp_path / "journal.json", "r", "test")
    token = current_journal.set(journal)
    output_token = current_result_budget.set(
        ResultBudget(ToolResultStore(tmp_path / "out", max_bytes=1, quota_bytes=1), "r", 1000)
    )
    scheduler = scheduler_for(registry, bus, manager)
    try:
        result = await scheduler.execute([call(0)], run_id="r")
        assert result[0].error_type == "output_storage_error" and count == 1
        assert journal.data["operations"]["0"]["state"] == "completed"
        current_result_budget.set(None)
        replay = await scheduler.execute([call(0)], run_id="r")
        assert replay == result and count == 1
        task = asyncio.create_task(scheduler.execute([call(1, wait=True)], run_id="r"))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert journal.data["operations"]["1"]["state"] == "unknown"
        unknown = await scheduler.execute([call(1, wait=True)], run_id="r")
        assert unknown[0].error_type == "unknown_outcome" and count == 2
        timeout = await invoke_tool(
            registry, call(2, wait=True), bus, "r", timeout=0.25, permission_manager=manager
        )
        assert timeout.error_type == "timeout"
        assert journal.data["operations"]["2"]["state"] == "unknown"
    finally:
        current_journal.reset(token)
        current_result_budget.reset(output_token)


# 功能：验证并行执行期间上下文保持完整且一次收齐后才调用下一轮模型
# 设计：使用真实 Loop 与网关，第二次模型调用时检查工具协议与结果顺序
async def test_loop_collects_before_next_model(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(ProbeTool(tmp_path), trusted_concurrency=True)
    bus = EventBus()
    manager = approving_manager(bus)
    manager.project_root = tmp_path
    context = ExecutionContext(run_id="r", goal="test", max_steps=3)

    class Provider:
        count = 0

        # 第二轮只在完整的有序结果入库后才允许返回结束消息
        async def chat(self, **kwargs):
            self.count += 1
            if self.count == 1:
                return LlmResponse("tool_use", tool_calls=[call(0, "a"), call(1, "b")])
            validate_messages(context.messages)
            results = context.messages[-1]["content"]
            assert [r["tool_use_id"] for r in results] == ["0", "1"]
            assert [r["content"] for r in results] == ["a", "b"]
            return LlmResponse("end_turn", text="done")

    loop = AgentLoop(
        Provider(),
        registry,
        bus,
        permission_manager=manager,
        tool_execution_config=ToolExecutionConfig(parallel_enabled=True),
    )
    await loop.run(context)
    assert context.status == "success" and context.step == 2


# 功能：验证同目录多存储实例在线程竞争中也不会突破实际磁盘配额
# 设计：并行写二十个结果且把元数据计入配额，检查成功数及目录总字节数
def test_storage_quota_atomic(tmp_path: Path) -> None:
    stores = [ToolResultStore(tmp_path / "out", max_bytes=1, quota_bytes=6000) for _ in range(2)]

    # 两个实例必须共享同一服务目录锁
    def write(index):
        return stores[index % 2].bound(ToolResult("x" * 2000), "r", str(index), 1000)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(write, range(20)))
    assert sum(p.stat().st_size for p in stores[0].root.iterdir()) <= 6000
    assert 0 < sum(not r.is_error for r in results) < 20


# 功能：验证线程中的日志准备不会丢失操作或同时创建相同操作号
# 设计：同一日志并发准备和更新三十个 ID，再竞争一个 ID 检查唯一登记
def test_journal_atomic(tmp_path: Path) -> None:
    journal = RunJournal(tmp_path / "checkpoint.json", "r", "test")

    # 每个线程执行完整状态迁移以覆盖读改写保护
    def write(index):
        journal.prepare(str(index), "probe", {"index": index})
        journal.update(str(index), "completed", output=str(index))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(30)))
        previous = list(pool.map(lambda _: journal.prepare("same", "probe", {}), range(10)))
    assert sum(result is None for result in previous) == 1
    assert len(json.loads(journal.path.read_text())["operations"]) == 31


# 功能：验证文件 I/O 取消后会等待工作线程释放才结束调用
# 设计：用线程事件控制释放时机，防止取消提前归还执行槽导致线程数量失控
async def test_thread_cancel_drains_worker() -> None:
    started = threading.Event()
    release = threading.Event()

    # 模拟有界但尚未完成的文件系统操作
    def work():
        started.set()
        release.wait(timeout=2)
        return "done"

    task = asyncio.create_task(run_file_io(work))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


# 功能：验证已信任资源工具的实际执行保持读写和写写顺序
# 设计：两个不同文件写调用可重叠，后续同文件读写必须观察到各自前序版本
async def test_resource_scoped_execution(tmp_path: Path) -> None:
    tool = ProbeTool(tmp_path)
    tool.concurrency_policy = "resource_scoped"
    active, peak = 0, 0

    # 文件内容提供可见的顺序证据，等待点让非冲突写操作具备重叠机会
    async def invoke(params):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            path = tmp_path / params["path"]
            if params.get("write"):
                await asyncio.sleep(0.01)
                path.write_text(str(params["value"]))
            return ToolResult(path.read_text())
        finally:
            active -= 1

    tool.invoke = invoke
    registry = ToolRegistry()
    registry.register(tool, trusted_concurrency=True)
    bus = EventBus()
    manager = approving_manager(bus)
    manager.project_root = tmp_path
    scheduler = scheduler_for(registry, bus, manager, policy="resource_scoped")
    calls = [
        call(0, "a", write=True, value=1),
        call(1, "b", write=True, value=2),
        call(2, "a"),
        call(3, "a", write=True, value=3),
        call(4, "a"),
    ]
    results = await scheduler.execute(calls, run_id="r")
    assert [r.content for r in results] == ["1", "2", "1", "3", "3"]
    assert peak == 2


# 功能：验证操作号绑定工具身份且并行审批状态不会提前变成运行中
# 设计：直接构造重放身份变化和两个待审批操作，检查持久化状态而非事件顺序
def test_journal_identity_and_approval_state(tmp_path: Path) -> None:
    journal = RunJournal(tmp_path / "checkpoint.json", "r", "test")
    journal.prepare("a", "probe", {}, identity="v1")
    journal.prepare("b", "probe", {}, identity="v1")
    journal.approval_wait("a", True)
    journal.approval_wait("b", True)
    journal.approval_wait("a", False)
    assert journal.data["state"] == "waiting_approval"
    journal.approval_wait("b", False)
    assert journal.data["state"] == "running"
    with pytest.raises(ValueError, match="identity changed"):
        journal.prepare("a", "probe", {}, identity="v2")
    with pytest.raises(ValueError, match="different input"):
        journal.prepare("a", "probe", {"different": True}, identity="v1")


# 功能：验证变更工具版本后的同操作号恢复返回未决错误且不执行新工具
# 设计：先完成并持久化一次调用，再重建不同版本注册表模拟重启升级
async def test_recovery_checks_tool_identity(tmp_path: Path) -> None:
    journal = RunJournal(tmp_path / "checkpoint.json", "r", "test")
    token = current_journal.set(journal)
    bus = EventBus()
    manager = approving_manager(bus)
    manager.project_root = tmp_path
    registry = ToolRegistry()
    registry.register(ProbeTool(tmp_path), trusted_concurrency=True)
    try:
        result = await scheduler_for(registry, bus, manager).execute([call(0)], run_id="r")
        assert not result[0].is_error
        changed = ProbeTool(tmp_path)
        changed.version = "2"

        # 身份不符必须在工具执行之前失败
        async def unexpected(params):
            raise AssertionError("changed operation replayed")

        changed.invoke = unexpected
        replacement = ToolRegistry()
        replacement.register(changed, trusted_concurrency=True)
        result = await scheduler_for(replacement, bus, manager).execute([call(0)], run_id="r")
        assert result[0].error_type == "unknown_outcome"
        assert journal.data["operations"]["0"]["state"] == "completed"
    finally:
        current_journal.reset(token)
