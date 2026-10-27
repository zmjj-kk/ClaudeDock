from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from claudedock.core.security.capabilities import Capability
from claudedock.core.tools.base import BaseTool, ToolResult
from claudedock.core.tools.paths import SafePath, resolve_inside, write_inside

_MAX_BYTES = 1 * 1024 * 1024  # 1 MB


class WriteFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    content: str


class WriteFileTool(BaseTool):
    capabilities = frozenset({Capability.WRITE})
    params_model = WriteFileParams
    name = "write_file"
    description = (
        "Write text content to a file, creating it (and any parent directories) if it "
        "does not exist, or overwriting it if it does. "
        "Path must be relative to the current working directory. "
        "Content size is limited to 1 MB."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file (relative to current working directory).",
            },
            "content": {
                "type": "string",
                "description": "Text content to write.",
            },
        },
        "required": ["path", "content"],
    }

    # 可选注入工作根目录（默认进程 cwd），便于测试隔离
    def __init__(self, root: Path | None = None) -> None:
        self._root = root

    # 写入文件内容；超 1MB 拒绝；路径解析后越界返回 permission_denied；自动创建父目录
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = WriteFileParams.model_validate(params)
        path_str = p.path
        content = p.content

        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_BYTES:
            return ToolResult(
                content=f"content too large: {len(encoded)} bytes (limit 1 MB)",
                is_error=True,
                error_type="runtime_error",
            )

        safe: SafePath = resolve_inside(self._root or Path.cwd(), path_str)
        if safe.path is None:
            return ToolResult(
                content=safe.error or "path rejected",
                is_error=True,
                error_type="permission_denied",
            )
        path = safe.path

        write_inside(self._root or Path.cwd(), path, content)

        return ToolResult(content=f"wrote {len(encoded)} bytes to {path_str}")
