from __future__ import annotations

import json
from typing import TYPE_CHECKING

from claudedock.core.security.capabilities import Capability
from claudedock.core.tools.base import BaseTool, ToolResult

if TYPE_CHECKING:
    from claudedock.core.tools.registry import ToolRegistry


class ToolSearchTool(BaseTool):
    capabilities = frozenset({Capability.READ})
    name = "tool_search"
    risk_level = "read"
    idempotent = True
    description = (
        "Discover authorized deferred tools by query. Search returns metadata only; "
        "resolve an exact returned name to make its schema available for this run."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "maxLength": 256},
            "resolve": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        },
        "additionalProperties": False,
    }

    # 绑定当前运行已过滤权限白名单的独立注册表
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    # 搜索不泄漏未注册工具，解锁只允许已授权的确定名称
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        try:
            names = params.get("resolve")
            if isinstance(names, list):
                resolved = self.registry.resolve_tools([str(name) for name in names])
                return ToolResult(json.dumps({"resolved": resolved}, ensure_ascii=False))
            result = self.registry.discover(str(params.get("query", "")))
            return ToolResult(json.dumps({"tools": result}, ensure_ascii=False))
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True, error_type="schema_error")
