from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from kama_claude.core.security.capabilities import Capability
from kama_claude.core.storage import atomic_write
from kama_claude.core.tools.base import BaseTool, ToolResult


class ToolResultRef(BaseModel):
    schema_version: int = 1
    result_id: str
    run_id: str
    tool_use_id: str
    size_bytes: int
    digest: str
    created_at: float
    expires_at: float


class ToolResultStore:
    # 固定会话私有目录与硬配额，不接受模型提供的存储路径
    def __init__(
        self, root: Path, *, max_bytes: int = 65536, preview_chars: int = 4000,
        quota_bytes: int = 64 * 1024 * 1024, retention_days: int = 30,
    ) -> None:
        if root.is_symlink():
            raise ValueError("tool result root cannot be a symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = root.resolve()
        self.max_bytes = max_bytes
        self.preview_chars = preview_chars
        self.quota_bytes = quota_bytes
        self.retention_days = retention_days

    # 仅返回私有目录内合法结果标识对应的文件
    def _path(self, result_id: str) -> Path:
        if len(result_id) != 32 or any(c not in "0123456789abcdef" for c in result_id):
            raise ValueError("invalid result id")
        path = self.root / result_id
        if path.is_symlink() or path.resolve().parent != self.root:
            raise ValueError("tool result path escapes storage")
        return path

    # 超限结果原子外置后返回预览；失败返回短状态且不重新执行工具
    def bound(
        self, result: ToolResult, run_id: str, tool_use_id: str, remaining: int,
    ) -> ToolResult:
        text = result.content
        size = len(text.encode("utf-8"))
        limit = min(self.max_bytes, max(0, remaining))
        if size <= limit:
            return result
        try:
            used = sum(p.stat().st_size for p in self.root.iterdir() if p.is_file())
            if size + used > self.quota_bytes:
                raise OSError("tool result storage quota exceeded")
            result_id = uuid.uuid4().hex
            path = self._path(result_id)
            ref = ToolResultRef(
                result_id=result_id, run_id=run_id, tool_use_id=tool_use_id,
                size_bytes=size, digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                created_at=time.time(), expires_at=time.time() + self.retention_days * 86400,
            )
            atomic_write(path, text)
            atomic_write(path.with_suffix(".json"), ref.model_dump_json())
            preview_limit = min(self.preview_chars, max(0, remaining // 4 - 256))
            preview = text[:preview_limit]
            return ToolResult(
                content=preview + f"\n[tool-result-ref:{result_id}; bytes={size}; "
                f"sha256={ref.digest}; read_tool_result(result_id, offset, max_bytes)]",
                is_error=result.is_error, error_type=result.error_type,
            )
        except (OSError, ValueError):
            return ToolResult(
                content="Tool executed; output persistence failed. Outcome must be verified; "
                "do not repeat side effects.", is_error=True, error_type="output_storage_error",
            )

    # 读取绑定会话的结果并限制分页，校验期限与原文摘要
    def read(self, result_id: str, offset: int = 0, max_bytes: int = 8192) -> str:
        if offset < 0 or not 1 <= max_bytes <= 16384:
            raise ValueError("invalid result page")
        path = self._path(result_id)
        meta = path.with_suffix(".json")
        if meta.is_symlink():
            raise ValueError("invalid result metadata")
        ref = ToolResultRef.model_validate_json(meta.read_text(encoding="utf-8"))
        if ref.result_id != result_id or ref.expires_at <= time.time():
            raise ValueError("tool result expired or invalid")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != ref.digest:
                raise ValueError("tool result checksum mismatch")
            stream.seek(offset)
            data = stream.read(max_bytes)
        return json.dumps(
            {"offset": offset, "next_offset": offset + len(data), "total_bytes": ref.size_bytes,
             "text": data.decode("utf-8", errors="replace")}, ensure_ascii=False,
        )


@dataclass
class ResultBudget:
    store: ToolResultStore
    run_id: str
    remaining: int

    # 所有工具共享本轮预览额度，原文只外置一次
    def bound(self, result: ToolResult, tool_use_id: str) -> ToolResult:
        bounded = self.store.bound(result, self.run_id, tool_use_id, self.remaining)
        self.remaining = max(0, self.remaining - len(bounded.content.encode("utf-8")))
        return bounded


current_result_budget: ContextVar[ResultBudget | None] = ContextVar(
    "tool_result_budget", default=None,
)


class ReadToolResultTool(BaseTool):
    capabilities = frozenset({Capability.READ})
    name = "read_tool_result"
    risk_level = "read"
    idempotent = True
    description = "Read a bounded byte page of an externalized result in this session."
    input_schema = {
        "type": "object",
        "properties": {
            "result_id": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": 16384},
        },
        "required": ["result_id"],
        "additionalProperties": False,
    }

    # 绑定当前会话存储，模型无法切换会话或任意文件路径
    def __init__(self, store: ToolResultStore) -> None:
        self.store = store

    # 经常规工具权限检查后读取分页，损坏与过期返回受控错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        try:
            return ToolResult(self.store.read(
                str(params["result_id"]), int(str(params.get("offset", 0))),
                int(str(params.get("max_bytes", 8192))),
            ))
        except (OSError, ValueError) as exc:
            return ToolResult(str(exc), is_error=True, error_type="result_unavailable")
