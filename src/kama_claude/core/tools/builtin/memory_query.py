from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from kama_claude.core.memory.store import MemoryStore
from kama_claude.core.security.capabilities import Capability
from kama_claude.core.tools.base import BaseTool, ToolResult


class MemoryQueryParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: Literal["preference", "decision", "fact", "knowledge"] | None = None
    project: str | None = None
    tags: list[str] = []
    limit: int = 10


class MemoryQueryTool(BaseTool):
    capabilities = frozenset({Capability.READ})
    params_model = MemoryQueryParams
    # 只读工具：精确查询是纯读操作，runtime_error 可安全重试
    retryable_errors = frozenset({"rate_limited", "runtime_error"})
    risk_level = "read"
    idempotent = True
    name = "memory_query"
    description = (
        "Precisely query structured long-term memory by category, project, or tags. "
        "Use this to list stored preferences, decisions, or facts matching exact criteria. "
        "Unlike memory_search (semantic), this returns entries that match the given filters."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["preference", "decision", "fact", "knowledge"],
                "description": "Filter by memory category.",
            },
            "project": {
                "type": "string",
                "description": "Filter by project annotation (empty = global).",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Match any of these tags.",
            },
            "limit": {
                "type": "integer",
                "description": "Max entries to return (default 10).",
            },
        },
        "required": [],
    }

    # 绑定 MemoryStore
    def __init__(self, store: MemoryStore, *, project: str | None = None) -> None:
        self._store = store
        self._project = project

    # 精确查询并返回结构化条目
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = MemoryQueryParams.model_validate(params)
        if self._project is not None and p.project not in (None, "", self._project):
            return ToolResult("Project scope denied", is_error=True, error_type="permission_denied")
        projects = (
            [self._project, ""] if self._project is not None and p.project is None else [p.project]
        )
        entries = []
        for project in projects:
            entries.extend(
                await self._store.query(
                    kind=p.kind,
                    project=project,
                    tags=p.tags or None,
                    limit=max(1, min(p.limit, 50)),
                )
            )
        entries = list({entry.id: entry for entry in entries}.values())[: max(1, min(p.limit, 50))]
        if not entries:
            return ToolResult(content="No matching memories found.")
        lines = [f"[{i}] ({e.kind}) {e.content}" for i, e in enumerate(entries, 1)]
        return ToolResult(content="\n".join(lines))
