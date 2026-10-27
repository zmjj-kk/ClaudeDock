from __future__ import annotations

import os
from itertools import islice
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from claudedock.core.security.capabilities import Capability
from claudedock.core.tools.base import BaseTool, ToolResult
from claudedock.core.tools.concurrency import ResourceClaim, file_claim, run_file_io
from claudedock.core.tools.paths import SafePath, directory_descriptor, resolve_inside

_MAX_DEPTH = 4
_MAX_ENTRIES = 200


class ListDirParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str = "."
    max_depth: int = Field(default=2, ge=1, le=_MAX_DEPTH)


class ListDirTool(BaseTool):
    concurrency_policy = "parallel_read"
    capabilities = frozenset({Capability.READ})
    params_model = ListDirParams
    # 只读工具：runtime_error（如瞬时文件缺失）可安全重试，不会产生副作用
    retryable_errors = frozenset({"rate_limited", "runtime_error"})
    risk_level = "read"
    idempotent = True
    name = "list_dir"
    description = (
        "List the contents of a directory as a tree. "
        "Path must be relative to the current working directory. "
        "Hidden entries (starting with .) are included. "
        f"Maximum depth is {_MAX_DEPTH}, maximum total entries is {_MAX_ENTRIES}."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the directory (default '.').",
            },
            "max_depth": {
                "type": "integer",
                "description": f"How many levels deep to recurse (default 2, max {_MAX_DEPTH}).",
            },
        },
        "required": [],
    }

    # 可选注入工作根目录（默认进程 cwd），便于测试隔离
    def __init__(self, root: Path | None = None) -> None:
        self._root = root

    # 以树状格式列出目录内容，深度和条数有上限；路径越界返回 permission_denied
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return await run_file_io(lambda: self._invoke_sync(params))

    # 使用与实际读取一致的安全根目录推导只读资源
    def resource_claims(self, params: dict[str, object]) -> tuple[ResourceClaim, ...] | None:
        claim = file_claim(self._root or Path.cwd(), params.get("path", "."), "read")
        return (claim,) if claim is not None else None

    # 在线程中完成原有路径检查和描述符访问，保持沙箱语义
    def _invoke_sync(self, params: dict[str, object]) -> ToolResult:
        p = ListDirParams.model_validate(params)
        path_str = p.path
        max_depth = p.max_depth

        safe: SafePath = resolve_inside(self._root or Path.cwd(), path_str)
        if safe.path is None:
            return ToolResult(
                content=safe.error or "path rejected",
                is_error=True,
                error_type="permission_denied",
            )
        root = safe.path

        if not root.exists():
            raise FileNotFoundError(f"no such directory: {path_str}")
        # root 已由 resolve_inside 解析为绝对路径，直接显示其绝对路径不泄露外部信息
        if not root.is_dir():
            raise NotADirectoryError(f"not a directory: {path_str}")

        lines: list[str] = [str(root) + "/"]
        count = 0

        # 在目录描述符下有界枚举，拒绝检查之后被置换的符号链接
        def _walk(fd: int, depth: int, prefix: str, directory_path: Path) -> None:
            nonlocal count
            if depth > max_depth or count >= _MAX_ENTRIES:
                return
            with os.scandir(fd) as iterator:
                entries = sorted(
                    islice(iterator, _MAX_ENTRIES + 1),
                    key=lambda e: (e.is_file(follow_symlinks=False), e.name),
                )
            for i, entry in enumerate(entries):
                if count >= _MAX_ENTRIES:
                    lines.append(f"{prefix}... (truncated)")
                    return
                is_dir = entry.is_dir(follow_symlinks=False)
                connector = "└── " if i == len(entries) - 1 else "├── "
                suffix = "/" if is_dir else ""
                lines.append(f"{prefix}{connector}{entry.name}{suffix}")
                count += 1
                if (
                    is_dir
                    and depth < max_depth
                    and resolve_inside(
                        self._root or Path.cwd(), str(directory_path / entry.name)
                    ).path
                    is not None
                ):
                    try:
                        child = os.open(
                            entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                        )
                    except OSError:
                        continue
                    try:
                        extension = "    " if i == len(entries) - 1 else "│   "
                        _walk(child, depth + 1, prefix + extension, directory_path / entry.name)
                    finally:
                        os.close(child)

        with directory_descriptor(self._root or Path.cwd(), root) as descriptor:
            _walk(descriptor, 1, "", root)
        return ToolResult(content="\n".join(lines))
