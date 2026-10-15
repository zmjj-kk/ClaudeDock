from __future__ import annotations

from kama_claude.core.memory.store import MemoryStore
from kama_claude.core.security.capabilities import Capability
from kama_claude.core.tools.base import BaseTool, ToolResult


class MemoryRetireTool(BaseTool):
    capabilities = frozenset({Capability.WRITE})
    name = "memory_retire"
    description = "Expire a memory in the current project so it no longer influences recall."
    risk_level = "write"
    idempotent = True
    input_schema = {
        "type": "object",
        "properties": {"memory_id": {"type": "string", "maxLength": 128}},
        "required": ["memory_id"],
        "additionalProperties": False,
    }

    # 绑定程序提供的项目范围，模型不能选择其他项目
    def __init__(self, store: MemoryStore, project: str) -> None:
        self.store = store
        self.project = project

    # 通过常规审批入口撤销记忆，不将撤销当作已核验新事实
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        try:
            await self.store.retire(str(params["memory_id"]), self.project)
            return ToolResult("Memory expired")
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True, error_type="permission_denied")
