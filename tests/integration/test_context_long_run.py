from __future__ import annotations

from pathlib import Path
from typing import Any

from claudedock.core.compact.token_budget import ModelCapacity
from claudedock.core.compact.tool_result_store import ReadToolResultTool, ToolResultStore
from claudedock.core.config import CompactionConfig
from claudedock.core.context import ExecutionContext
from claudedock.core.events.bus import EventBus
from claudedock.core.llm.context_errors import ContextWindowExceeded
from claudedock.core.llm.types import LlmResponse, ToolCallBlock
from claudedock.core.loop import AgentLoop
from claudedock.core.tools.base import BaseTool, ToolResult
from claudedock.core.tools.registry import ToolRegistry
from tests.security_helpers import approving_manager


class LargeOutputTool(BaseTool):
    name = "large_output"
    description = "Return a large observation"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}
    calls = 0

    # 返回大输出以验证真实工具调用链的统一外置
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        self.calls += 1
        return ToolResult("evidence" * 262144)


class ReplayProvider:
    context_capacity = ModelCapacity(200000)

    # 在记录输入的同时返回固定工具调用与最终回复
    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    # 不调用网络，第二轮直接检查模型实际看到的结果预览
    async def chat(self, **kwargs: Any) -> LlmResponse:
        self.calls.append(kwargs["messages"])
        if len(self.calls) == 1:
            return LlmResponse("tool_use", tool_calls=[ToolCallBlock("call", "large_output", {})])
        assert len(str(kwargs["messages"])) < 10000
        return LlmResponse("end_turn", text="done")


# 功能：验证真实循环处理两兆输出，原文不进入事件或下一轮上下文
# 设计：使用真实 Registry、invoke_tool 与 EventBus，仅替换模型确保零计费
async def test_large_output_through_loop(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path / "results")
    registry = ToolRegistry()
    tool = LargeOutputTool()
    registry.register(tool)
    registry.register(ReadToolResultTool(store))
    provider = ReplayProvider()
    bus = EventBus()
    events: list[Any] = []

    # 收集事件以断言审计关联保留而巨型原文未重复泄漏
    async def receive(event: Any) -> None:
        events.append(event)

    bus.subscribe(receive)
    context = ExecutionContext("run", "inspect output", 5)
    loop = AgentLoop(
        provider, registry, bus, result_store=store, permission_manager=approving_manager(bus),
        compaction_config=CompactionConfig(preflight_mode="enforce"),
    )
    await loop.run(context)
    assert context.status == "success" and tool.calls == 1
    finished = [e for e in events if e.type == "tool.call_finished"]
    assert len(finished) == 1
    assert all(len(str(e.model_dump())) < 10000 for e in finished)


# 功能：验证明确超限无法恢复时有限失败，不重新执行任何工具
# 设计：提供者直接返回容量异常，并使用空注册表排除工具重放
async def test_context_error_fails_finitely() -> None:
    class OverflowProvider:
        context_capacity = ModelCapacity(200000)
        calls = 0

        # 以明确错误类型模拟供应商上下文拒绝
        async def chat(self, **kwargs: Any) -> LlmResponse:
            self.calls += 1
            raise ContextWindowExceeded("overflow")

    provider = OverflowProvider()
    context = ExecutionContext("run", "goal", 5)
    await AgentLoop(provider, ToolRegistry(), EventBus()).run(context)
    assert provider.calls == 1 and context.reason == "context_too_large"

# 功能：验证新增超长用户输入在发送模型前被硬拦截
# 设计：给模型极小已知窗口并记录调用次数，确保失败发生在网络边界之前
async def test_long_user_input_blocked_before_provider() -> None:
    provider = ReplayProvider()
    provider.context_capacity = ModelCapacity(1000, 100)
    context = ExecutionContext("run", "长输入" * 1000, 5)
    await AgentLoop(
        provider, ToolRegistry(), EventBus(),
        compaction_config=CompactionConfig(preflight_mode="enforce"),
    ).run(context)
    assert context.reason == "context_too_large" and not provider.calls


# 功能：验证未知模型在强制模式不会以零占用静默发送
# 设计：未声明容量的旧提供者仍可用于影子模式，但强制模式必须明确拒绝
async def test_unknown_capacity_blocked() -> None:
    class UnknownProvider:
        calls = 0

        # 仅记录调用次数，正常情况下此方法不应被执行
        async def chat(self, **kwargs: Any) -> LlmResponse:
            self.calls += 1
            return LlmResponse("end_turn", text="done")

    provider = UnknownProvider()
    context = ExecutionContext("run", "goal", 5)
    await AgentLoop(
        provider, ToolRegistry(), EventBus(),
        compaction_config=CompactionConfig(preflight_mode="enforce"),
    ).run(context)
    assert context.reason == "context_window_unknown" and provider.calls == 0
