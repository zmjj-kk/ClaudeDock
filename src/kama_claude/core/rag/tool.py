from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from kama_claude.core.rag.store import RagStore
from kama_claude.core.tools.base import BaseTool, ToolResult


class RagSearchParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    query: str
    top_k: int = 3


class RagSearchTool(BaseTool):
    params_model = RagSearchParams
    # 只读工具：runtime_error（如瞬时 IO 失败）可安全重试，不会产生副作用
    retryable_errors = frozenset({"rate_limited", "runtime_error"})
    name = "rag_search"
    description = (
        "Search the local knowledge base for the chunks most relevant to a query. "
        "Use this to retrieve project or domain knowledge before answering. "
        "Returns matched chunks with their source file paths."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query.",
            },
            "top_k": {
                "type": "integer",
                "description": "Number of chunks to return (default 3).",
            },
        },
        "required": ["query"],
    }

    # 绑定 RagStore 与默认 top_k
    def __init__(self, store: RagStore, default_top_k: int = 3) -> None:
        self._store = store
        self._default_top_k = default_top_k

    # 执行检索并返回带来源的文本结果
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = RagSearchParams.model_validate(params)
        top_k = p.top_k or self._default_top_k
        chunks = self._store.search(p.query, top_k)
        if not chunks:
            return ToolResult(content="No matching chunks found in the knowledge base.")
        lines = [f"[{i}] {c.source}\n{c.text}" for i, c in enumerate(chunks, 1)]
        return ToolResult(content="\n\n".join(lines))
