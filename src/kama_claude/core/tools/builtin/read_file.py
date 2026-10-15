from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from kama_claude.core.security.capabilities import Capability
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.paths import SafePath, read_inside, resolve_inside

_MAX_BYTES = 512 * 1024  # 512 KB


class ReadFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str


class ReadFileTool(BaseTool):
    capabilities = frozenset({Capability.READ})
    params_model = ReadFileParams
    # 只读工具：runtime_error（如瞬时文件缺失）可安全重试，不会产生副作用
    retryable_errors = frozenset({"rate_limited", "runtime_error"})
    risk_level = "read"
    idempotent = True
    name = "read_file"
    description = (
        "Read the text content of a file. "
        "Path must be relative to the current working directory. "
        "Files larger than 512 KB are truncated."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file (relative to current working directory).",
            }
        },
        "required": ["path"],
    }

    # 可选注入工作根目录（默认进程 cwd），便于测试隔离；invoke 用其约束路径
    def __init__(self, root: Path | None = None) -> None:
        self._root = root

    # 读取文件内容；超 512KB 截断；路径解析后越界返回 permission_denied
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        path_str = ReadFileParams.model_validate(params).path
        safe: SafePath = resolve_inside(self._root or Path.cwd(), path_str)
        if safe.path is None:
            return ToolResult(
                content=safe.error or "path rejected",
                is_error=True,
                error_type="permission_denied",
            )
        path = safe.path

        # 只读取上限 + 1 字节以判定截断，避免整文件载入内存（OOM 防御）
        raw = read_inside(self._root or Path.cwd(), path, _MAX_BYTES + 1)
        truncated = len(raw) > _MAX_BYTES
        text = raw[:_MAX_BYTES].decode("utf-8", errors="replace")
        if truncated:
            text += "\n[truncated]"

        return ToolResult(content=text)
