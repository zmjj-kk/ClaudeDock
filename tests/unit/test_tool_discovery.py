from __future__ import annotations

import json
from typing import Any

import pytest

from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.discovery import ToolSearchTool
from kama_claude.core.tools.registry import ToolRegistry


class ExampleTool(BaseTool):
    description = "A specialized authorized tool"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}

    # 为分档工具发现评估生成稳定名称
    def __init__(self, name: str) -> None:
        self.name = name

    # 返回固定结果，不执行文件或网络操作
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult("ok")


# 功能：验证二百工具延迟发现只暴露基础 Schema，解锁不得超出授权注册集合
# 设计：检查名称搜索、模式大小、解锁前执行拒绝和未注册工具负向路径
async def test_deferred_discovery_freezes_authorized_tools() -> None:
    registry = ToolRegistry()
    for index in range(200):
        registry.register(ExampleTool(f"special_{index}"))
    search = ToolSearchTool(registry)
    registry.register(search)
    registry.enable_deferred({"tool_search", "special_0"})
    assert len(registry.tool_schemas()) == 2
    assert registry.get("special_194") is None
    result = json.loads((await search.invoke({"query": "special_194"})).content)
    assert result["tools"][0]["name"] == "special_194"
    assert "input_schema" not in result["tools"][0]
    assert not (await search.invoke({"resolve": ["special_194"]})).is_error
    assert registry.get("special_194") is not None
    assert len(registry.tool_schemas()) == 3
    assert (await search.invoke({"resolve": ["unauthorized"]})).is_error
    with pytest.raises(ValueError, match="frozen"):
        registry.register(ExampleTool("late"))


# 功能：验证反复解析同名工具的 Schema 摘要稳定
# 设计：使用同一冻结快照解析两次，排除动态注册顺序对缓存边界的影响
def test_resolved_schema_hash_stable() -> None:
    registry = ToolRegistry()
    registry.register(ExampleTool("example"))
    registry.enable_deferred(set())
    assert registry.resolve_tools(["example"]) == registry.resolve_tools(["example"])

# 功能：验证运行中工具 Schema 被替换时拒绝执行，而非静默使用新参数契约
# 设计：冻结后修改实例模式，检查快照保持旧值且后端校验明确失败
async def test_schema_changes_rejected_during_run() -> None:
    registry = ToolRegistry()
    tool = ExampleTool("example")
    registry.register(tool)
    registry.enable_deferred({"example"})
    tool.input_schema = {"type": "object", "properties": {"new": {"type": "string"}}}
    with pytest.raises(ValueError, match="schema or identity changed"):
        registry.validate("example", {})
    assert registry.tool_schemas()[0]["input_schema"] == {"type": "object", "properties": {}}
