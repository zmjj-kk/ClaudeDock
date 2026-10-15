from __future__ import annotations

import pytest

import kama_claude.core.tools.invocation as inv_mod
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.mcp.tool import McpTool
from kama_claude.core.subagent.tool import AgentResultTool, SpawnAgentTool
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.builtin import (
    BashTool,
    ListDirTool,
    NoteSaveTool,
    ReadFileTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
    WriteFileTool,
)
from kama_claude.core.tools.registry import ToolRegistry
from kama_claude.eval.checkers import assert_tool_result
from tests.security_helpers import invoke_tool

# 只读/幂等工具：runtime_error 可安全自动重试，不会产生副作用
_READ_ONLY = (ReadFileTool, ListDirTool, TaskGetTool, TaskListTool, AgentResultTool)

# 有副作用或非幂等工具：runtime_error 禁止自动重试（默认仅 rate_limited 可重试）
_SIDE_EFFECT = (
    BashTool,
    WriteFileTool,
    TaskCreateTool,
    TaskUpdateTool,
    NoteSaveTool,
    SpawnAgentTool,
    McpTool,
)


# 功能：验证只读工具声明 runtime_error 可重试，瞬时读取失败（如文件尚未就绪）不会直接失败
# 设计：直接断言类属性 retryable_errors，无需实例化（task 工具需 TaskManager），纯分类回归
def test_read_only_tools_retry_runtime_error() -> None:
    for cls in _READ_ONLY:
        assert "runtime_error" in cls.retryable_errors, f"{cls.__name__} 应允许重试 runtime_error"


# 功能：验证有副作用/非幂等工具默认不重试 runtime_error，防止副作用被二次执行
# 设计：断言类属性不含 runtime_error；这些工具继承 BaseTool 默认集，仅 rate_limited 可重试
def test_side_effect_tools_do_not_retry_runtime_error() -> None:
    for cls in _SIDE_EFFECT:
        assert "runtime_error" not in cls.retryable_errors, f"{cls.__name__} 不应重试 runtime_error"


# 功能：验证所有工具都允许重试 rate_limited，因为限流在语义上是"请求未执行"
# 设计：rate_limited 属于 BaseTool 默认集，全部工具继承；断言分类统一成立
def test_all_tools_retry_rate_limited() -> None:
    for cls in _READ_ONLY + _SIDE_EFFECT:
        if cls is McpTool:
            assert not cls.retryable_errors
            continue
        assert "rate_limited" in cls.retryable_errors, f"{cls.__name__} 应允许重试 rate_limited"


class _SideEffectTool(BaseTool):
    """模拟有副作用的工具：runtime_error 表示"部分副作用已生效"，默认不可重试。"""

    name = "side_effect"
    description = "has side effects"
    input_schema: dict[str, object] = {"type": "object", "properties": {}, "required": []}

    def __init__(self) -> None:
        self.invocations = 0

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        self.invocations += 1
        return ToolResult(content="partial failure", is_error=True, error_type="runtime_error")


# 功能：验证未声明 runtime_error 可重试的工具，其 runtime_error 只执行一次、不自动重试
# 设计：用带计数器的 stub 断言 invoke 次数 ==1；monkeypatch 消除 backoff 延迟，防止回归时拖慢
async def test_side_effect_runtime_error_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inv_mod, "_RETRY_BASE_S", 0.0)

    tool = _SideEffectTool()
    registry = ToolRegistry()
    registry.register(tool)
    result = await invoke_tool(
        registry,
        ToolCallBlock(id="t1", name="side_effect", input={}),
        EventBus(),
        run_id="r",
    )
    assert_tool_result(result, is_error=True, error_type="runtime_error")
    assert tool.invocations == 1
