from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from kama_claude.core.memory.store import MemoryStore
from kama_claude.core.tools.base import BaseTool, ToolResult


class MemorySaveParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    content: str
    kind: Literal["preference", "decision", "fact", "knowledge"] = "fact"
    importance: float = 0.5
    tags: list[str] = []


class MemorySaveTool(BaseTool):
    params_model = MemorySaveParams
    name = "memory_save"
    # 价值过滤：描述显式列出值得存的类别与不值得存的类别，这是"存什么"原则的主要落地
    description = (
        "Save a durable, cross-session fact to long-term memory. "
        "Only save information that will be valuable in future sessions. "
        "Worth saving: (1) user preferences & habits (language style, tech stack, workflow) — "
        "use kind='preference'; (2) key conclusions & decisions from a task (plan changes, "
        "architecture refactors) — use kind='decision'; (3) high-frequency external knowledge "
        "(product docs, stable facts) — use kind='knowledge'; short structured facts use "
        "kind='fact'. Do NOT save trivial intermediate reasoning, verbose raw logs, or "
        "one-off temporary conversation. Set importance 0..1 by how often this will matter."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The durable fact, preference, or decision to remember.",
            },
            "kind": {
                "type": "string",
                "enum": ["preference", "decision", "fact", "knowledge"],
                "description": "Category that determines storage and retrieval.",
            },
            "importance": {
                "type": "number",
                "description": "Self-assessed importance 0.0 (low) to 1.0 (high). Default 0.5.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Structured tags for precise filtering later.",
            },
        },
        "required": ["content"],
    }

    # 绑定 MemoryStore，供写入路由到 sqlite / 向量层
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    # 校验非空后写入长期记忆；重复记忆返回已存在提示
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = MemorySaveParams.model_validate(params)
        content = p.content.strip()
        if not content:
            return ToolResult(
                content="empty content", is_error=True, error_type="runtime_error"
            )
        entry = await self._store.add(
            content,
            kind=p.kind,
            importance=p.importance,
            tags=p.tags,
        )
        if entry is None:
            return ToolResult(content="already remembered (duplicate)")
        return ToolResult(content=f"saved [{entry.kind}] {entry.id}")
