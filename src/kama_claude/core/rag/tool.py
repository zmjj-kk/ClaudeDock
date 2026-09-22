from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from kama_claude.core.rag.store import RagStore
from kama_claude.core.tools.base import BaseTool, ToolResult


class RagSearchParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    query: str
    top_k: int = 3
    # 元数据过滤：键=字段，值为字符串/列表（任一匹配）/范围字典（gte/lte/gt/lt）
    filter: dict[str, object] = {}


class RagSearchTool(BaseTool):
    params_model = RagSearchParams
    # 只读工具：runtime_error（如瞬时 IO 失败）可安全重试，不会产生副作用
    retryable_errors = frozenset({"rate_limited", "runtime_error"})
    name = "rag_search"
    # 基础描述：含决策规则与示例；由 __init__ 拼接主题清单与元数据词汇表
    description = (
        "Search the local knowledge base for the chunks most relevant to a query. "
        "Use this to retrieve project or domain knowledge before answering. "
        "Returns matched chunks with their source file paths. "
        "If the user's question constrains the answer by a metadata field "
        "(a department, a time range, or a specific source document), set that key "
        "in `filter` to restrict the search — only use values listed below. "
        "Leave `filter` empty when the question has no such constraint. "
        "Examples: '查研发部门的上线步骤' → filter={\"department\": \"研发\"}; "
        "'最近半年的更新' → filter={\"updated_at\": {\"gte\": \"2026-03\"}}."
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
            "filter": {
                "type": "object",
                "description": (
                    "Optional metadata filters. Keys are metadata field names. Values "
                    "are strings, arrays of strings (array = match any), or a range "
                    "object with gte/lte/gt/lt (e.g. {\"gte\": \"2026-03\"}). Use ONLY "
                    "values listed in the tool description."
                ),
            },
        },
        "required": ["query"],
    }

    # 绑定 RagStore 与默认 top_k，把主题清单与元数据词汇表拼进工具描述
    def __init__(self, store: RagStore, default_top_k: int = 3) -> None:
        self._store = store
        self._default_top_k = default_top_k
        manifest = store.manifest()
        if manifest:
            self.description = f"{self.description}\n\n{manifest}"
        mm = store.metadata_manifest()
        if mm:
            self.description = f"{self.description}\n\n{mm}"

    # 执行检索并返回带来源的文本结果
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = RagSearchParams.model_validate(params)
        top_k = p.top_k or self._default_top_k
        chunks = await self._store.search(p.query, top_k, filter=p.filter)
        if not chunks:
            return ToolResult(content="No matching chunks found in the knowledge base.")
        lines = [f"[{i}] {c.source}\n{c.text}" for i, c in enumerate(chunks, 1)]
        return ToolResult(content="\n\n".join(lines))
