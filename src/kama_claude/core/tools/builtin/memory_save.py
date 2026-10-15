from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from kama_claude.core.memory.store import MemoryStore
from kama_claude.core.security.capabilities import Capability
from kama_claude.core.tools.base import BaseTool, ToolResult


class MemorySaveParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    content: str
    kind: Literal["preference", "decision", "fact", "knowledge"] = "fact"
    importance: float = 0.5
    tags: list[str] = []
    project: str = ""
    expires_at: str | None = None


class MemorySaveTool(BaseTool):
    capabilities = frozenset({Capability.WRITE})
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
            "project": {
                "type": "string",
                "description": "Project annotation for scoped recall (empty = global).",
            },
            "expires_at": {
                "type": "string",
                "description": "Optional timezone-aware ISO expiry for volatile facts.",
                "maxLength": 64,
            },
        },
        "required": ["content"],
    }

    # 绑定 MemoryStore 与默认项目标识，模型未显式给 project 时落回当前项目
    def __init__(
        self,
        store: MemoryStore,
        default_project: str = "",
        *,
        scoped: bool = False,
        enforce_project: bool = False,
    ) -> None:
        self._store = store
        self._default_project = default_project
        self._scoped = scoped
        self._enforce_project = scoped or enforce_project

    # 校验非空后写入长期记忆；重复记忆返回已存在提示
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = MemorySaveParams.model_validate(params)
        content = p.content.strip()
        if not content:
            return ToolResult(content="empty content", is_error=True, error_type="runtime_error")
        if self._enforce_project and p.project not in ("", self._default_project):
            return ToolResult("Project scope denied", is_error=True, error_type="permission_denied")
        expiry = None
        if p.expires_at:
            try:
                parsed = datetime.fromisoformat(p.expires_at)
                if parsed.tzinfo is None:
                    raise ValueError("expiry requires timezone")
                expiry = parsed
            except ValueError:
                return ToolResult("Invalid expiry", is_error=True, error_type="schema_error")
        if self._scoped and p.kind in {"fact", "knowledge"}:
            maximum = datetime.now(UTC) + timedelta(days=7 if p.kind == "fact" else 30)
            expiry = min(expiry, maximum) if expiry is not None else maximum
        entry = await self._store.add(
            content,
            kind=p.kind,
            importance=p.importance,
            tags=p.tags,
            project=p.project or self._default_project,
            expires_at=expiry.isoformat() if expiry is not None else None,
        )
        if entry is None:
            return ToolResult(content="already remembered (duplicate)")
        return ToolResult(content=f"saved [{entry.kind}] {entry.id}")
