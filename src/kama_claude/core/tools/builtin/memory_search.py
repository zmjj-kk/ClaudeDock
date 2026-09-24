from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from kama_claude.core.memory.store import MemoryStore
from kama_claude.core.tools.base import BaseTool, ToolResult


class MemorySearchParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    query: str
    top_k: int = 5
    filter: dict[str, object] = {}


class MemorySearchTool(BaseTool):
    params_model = MemorySearchParams
    # 只读工具：检索是纯读操作，runtime_error 可安全重试
    retryable_errors = frozenset({"rate_limited", "runtime_error"})
    name = "memory_search"
    description = (
        "Semantically search long-term memory for facts relevant to the current task. "
        "Use this to recall durable user preferences, past decisions, or external "
        "knowledge before answering. Results are ranked by semantic similarity weighted "
        "by importance and recency. Constrain `filter` by kind/project/tags only when the "
        "question clearly asks for a specific category."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The semantic query for memory retrieval.",
            },
            "top_k": {
                "type": "integer",
                "description": "Number of memories to return (default 5).",
            },
            "filter": {
                "type": "object",
                "description": (
                    "Optional filters by memory kind/project/tags (string or array = match any)."
                ),
            },
        },
        "required": ["query"],
    }

    # 绑定 MemoryStore 与默认 top_k
    def __init__(self, store: MemoryStore, default_top_k: int = 5) -> None:
        self._store = store
        self._default_top_k = default_top_k

    # 语义检索并返回带类别与正文的结果列表
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = MemorySearchParams.model_validate(params)
        top_k = p.top_k or self._default_top_k
        entries = await self._store.search(p.query, top_k, filter=p.filter)
        if not entries:
            return ToolResult(content="No relevant memories found.")
        lines = [f"[{i}] ({e.kind}) {e.content}" for i, e in enumerate(entries, 1)]
        return ToolResult(content="\n".join(lines))
