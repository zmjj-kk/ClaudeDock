from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

import pytest

from claudedock.core.config import ClaudeDockConfig, ToolExecutionConfig, _apply_toml
from claudedock.core.events.bus import EventBus
from claudedock.core.llm.types import ToolCallBlock
from claudedock.core.tools.base import BaseTool, ToolResult
from claudedock.core.tools.concurrency import ResourceClaim, file_claim
from claudedock.core.tools.registry import ToolRegistry
from claudedock.core.tools.scheduler import ToolExecutionScheduler


class ProbeTool(BaseTool):
    name = "probe"
    description = "Controlled scheduling probe"
    concurrency_policy = "parallel_read"
    input_schema = {"type": "object", "properties": {"path": {"type": "string"}}}

    # 固定可信测试工具的资源根目录与可控执行回调
    def __init__(self, root: Path) -> None:
        self._root = root

    # 根据真实安全路径解析器声明资源，模式只由测试宿主指定
    def resource_claims(self, params: dict[str, object]) -> tuple[ResourceClaim, ...] | None:
        mode: Literal["read", "write"] = "write" if params.get("write") else "read"
        claim = file_claim(self._root, params.get("path", "a"), mode)
        return (claim,) if claim is not None else None

    # 单测由调度器回调控制执行，集成测试替换此方法
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult(str(params.get("path", "a")))


# 构造不携带调度特权的模型工具调用
def call(index: int, path: str = "a", **params: object) -> ToolCallBlock:
    return ToolCallBlock(str(index), "probe", {"path": path, **params})


# 功能：验证并发调用逆序完成后仍按输入顺序返回结果
# 设计：使用事件握手强制真实重叠与逆序，避免依赖计时阈值
async def test_overlap_and_order(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(ProbeTool(tmp_path), trusted_concurrency=True)
    second = asyncio.Event()
    finished: list[str] = []

    # 第二个调用完成后才允许第一个返回，串行实现将触发超时
    async def invoke(tc, slots):
        async with slots[0], slots[1]:
            if tc.id == "0":
                await second.wait()
            else:
                second.set()
            finished.append(tc.id)
            return ToolResult(tc.id)

    scheduler = ToolExecutionScheduler(
        registry,
        invoke,
        EventBus(),
        asyncio.Semaphore(2),
        ToolExecutionConfig(parallel_enabled=True),
    )
    results = await asyncio.wait_for(scheduler.execute([call(0), call(1)], run_id="r"), 1)
    assert finished == ["1", "0"]
    assert [r.content for r in results] == ["0", "1"]


@pytest.mark.parametrize(
    "enabled,shadow,trusted", [(False, False, True), (False, True, True), (True, False, False)]
)
# 功能：验证默认关闭、shadow 和未经宿主信任的工具均保持串行
# 设计：让工具显式自报并行能力，确认该声明无法越过配置和注册边界
async def test_serial_modes(tmp_path: Path, enabled: bool, shadow: bool, trusted: bool) -> None:
    registry = ToolRegistry()
    registry.register(ProbeTool(tmp_path), trusted_concurrency=trusted)
    active, peak = 0, 0

    # 记录真实重叠数量而不依赖总耗时
    async def invoke(tc, slots):
        nonlocal active, peak
        async with slots[0], slots[1]:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return ToolResult(tc.id)

    config = ToolExecutionConfig(parallel_enabled=enabled, shadow_enabled=shadow)
    scheduler = ToolExecutionScheduler(registry, invoke, EventBus(), asyncio.Semaphore(4), config)
    await scheduler.execute([call(0), call(1)], run_id="r")
    assert peak == 1


# 功能：验证冲突 DAG 保持读写和写写顺序并允许不相交资源重叠
# 设计：直接检查依赖关系，覆盖父子目录、路径别名和未知资源屏障
async def test_resource_dependencies(tmp_path: Path) -> None:
    tool = ProbeTool(tmp_path)
    tool.concurrency_policy = "resource_scoped"
    registry = ToolRegistry()
    registry.register(tool, trusted_concurrency=True)

    # 规划测试不应执行任何工具
    async def unused(tc, slots):
        raise AssertionError("planning executed a tool")

    config = ToolExecutionConfig(parallel_enabled=True, policy="resource_scoped")
    scheduler = ToolExecutionScheduler(registry, unused, EventBus(), asyncio.Semaphore(4), config)
    calls = [
        call(0, "dir", write=True),
        call(1, "dir/a"),
        call(2, "b", write=True),
        call(3, "./dir/a", write=True),
    ]
    assert scheduler.plan(calls) == [[], [0], [], [0, 1]]
    assert scheduler.plan([call(0, "../escape"), call(1)]) == [[], [0]]
    (tmp_path / "link").symlink_to(tmp_path / "dir")
    assert scheduler.plan([call(0, "dir", write=True), call(1, "link/a")]) == [[], [0]]
    (tmp_path / "file").write_text("a")
    (tmp_path / "alias").hardlink_to(tmp_path / "file")
    assert scheduler.plan([call(0, "alias"), call(1, "b")]) == [[], [0]]
    tool.concurrency_policy = "parallel_read"
    assert registry.concurrency_policy("probe") == "serialized"


# 功能：验证串行屏障不能被后续独立只读调用跨越
# 设计：把未知工具插在两个已信任读调用之间，检查全部前序依赖
async def test_barrier(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(ProbeTool(tmp_path), trusted_concurrency=True)

    # 不执行规划之外的工作
    async def unused(tc, slots):
        return ToolResult(tc.id)

    scheduler = ToolExecutionScheduler(
        registry,
        unused,
        EventBus(),
        asyncio.Semaphore(4),
        ToolExecutionConfig(parallel_enabled=True),
    )
    barrier = ToolCallBlock("barrier", "bash", {"command": "true"})
    assert scheduler.plan([call(0), call(1), barrier, call(3)]) == [[], [], [0, 1], [2]]


@pytest.mark.parametrize("ids", [["x", "x"], ["", "x"], [" ", "x"]])
# 功能：验证缺失或重复 ID 整批拒绝且不产生副作用
# 设计：执行回调始终报错，只有完全未进入执行路径才能通过
async def test_invalid_ids(tmp_path: Path, ids: list[str]) -> None:
    # 不允许无效批次触发工具调用
    async def unused(tc, slots):
        raise AssertionError("invalid batch executed")

    scheduler = ToolExecutionScheduler(ToolRegistry(), unused, EventBus(), asyncio.Semaphore(1))
    results = await scheduler.execute([ToolCallBlock(uid, "probe", {}) for uid in ids], run_id="r")
    assert len(results) == len(ids)
    assert all(r.error_type == "protocol_error" for r in results)


# 功能：验证批次数量超限时不创建工具任务
# 设计：以两个调用触发上限一，确认每个调用得到同类配对错误
async def test_batch_limit() -> None:
    # 队列限额在创建任务之前生效
    async def unused(tc, slots):
        raise AssertionError("overflow executed")

    scheduler = ToolExecutionScheduler(
        ToolRegistry(),
        unused,
        EventBus(),
        asyncio.Semaphore(1),
        ToolExecutionConfig(max_calls_per_batch=1),
    )
    results = await scheduler.execute([call(0), call(1)], run_id="r")
    assert [r.error_type for r in results] == ["batch_limit", "batch_limit"]


# 功能：验证单调用异常隔离且父任务取消时所有工具退出
# 设计：先执行异常与正常调用，再用事件确认两子任务均进入并被回收
async def test_errors_and_cancel(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(ProbeTool(tmp_path), trusted_concurrency=True)
    started = asyncio.Event()
    active = 0

    # 异常不影响独立调用，阻塞调用在取消后记录资源释放
    async def invoke(tc, slots):
        nonlocal active
        if tc.input.get("fail"):
            raise RuntimeError("secret details")
        if not tc.input.get("wait"):
            return ToolResult("ok")
        active += 1
        if active == 2:
            started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    scheduler = ToolExecutionScheduler(
        registry,
        invoke,
        EventBus(),
        asyncio.Semaphore(4),
        ToolExecutionConfig(parallel_enabled=True),
    )
    results = await scheduler.execute([call(0, fail=True), call(1)], run_id="r")
    assert results[0].error_type == "unknown_outcome" and results[1].content == "ok"
    assert "secret" not in results[0].content
    task = asyncio.create_task(
        scheduler.execute([call(2, wait=True), call(3, wait=True)], run_id="r")
    )
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0


@pytest.mark.parametrize(
    "values",
    [
        {"parallel_enabled": 1},
        {"max_calls_per_batch": True},
        {"max_concurrency_per_run": 0},
        {"max_pending_approvals": 257},
        {"policy": "unsafe"},
        {"unknown": True},
    ],
)
# 功能：验证调度配置拒绝无界值、伪整数和未知字段
# 设计：走生产 TOML 配置入口，覆盖外部输入而不是只测数据类构造
def test_config_validation(values: dict[str, object]) -> None:
    with pytest.raises(SystemExit):
        _apply_toml(ClaudeDockConfig(), {"tool_execution": values})


# 功能：验证未知 MCP 工具和内置工具子类不能凭继承的元数据获得并行权限
# 设计：显式请求宿主信任仍不接受远端自报，精确类型白名单拒绝未经审核的子类
def test_external_metadata_stays_serialized(tmp_path: Path) -> None:
    tool = ProbeTool(tmp_path)
    tool.server_id = "remote"
    registry = ToolRegistry()
    registry.register(tool, trusted_concurrency=True)
    assert registry.concurrency_policy("probe") == "serialized"

    from claudedock.core.tools.builtin.read_file import ReadFileTool

    class UnreviewedRead(ReadFileTool):
        pass

    registry = ToolRegistry()
    registry.register(UnreviewedRead(tmp_path))
    assert registry.concurrency_policy("read_file") == "serialized"
