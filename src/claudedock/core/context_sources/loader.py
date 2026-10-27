from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from claudedock.core.security.redaction import redact


@dataclass(frozen=True)
class InstructionSource:
    source_path: str
    source_hash: str
    scope: str
    precedence: int
    mtime_ns: int
    content: str
    trust_level: str = "external"
    schema_version: int = 1


@dataclass
class InstructionSnapshot:
    sources: list[InstructionSource] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)

    # 导出不含正文的来源清单，供事件与检查点审计
    def manifest(self) -> list[dict[str, Any]]:
        return [
            {key: value for key, value in asdict(source).items() if key != "content"}
            for source in self.sources
        ]

    # 按明确顺序展示外部文件，不把文件内容提升为系统权限
    def render(self) -> str:
        return "\n\n".join(
            f"<external-instructions source={source.source_path!r} "
            f"scope={source.scope!r} precedence={source.precedence} "
            f"sha256={source.source_hash!r}>\n{source.content}\n</external-instructions>"
            for source in self.sources
        )


# 从已授权根目录逐级打开普通文件，拒绝父目录软链接竞态逃逸
def _open_within(root: Path, path: Path) -> int:
    parts = path.relative_to(root).parts
    if not parts:
        raise ValueError("instruction source must be a regular file")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory,
            )
            os.close(directory)
            directory = child
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    finally:
        os.close(directory)


class InstructionLoader:
    # 限定可信选择的根目录、总量、单文件与导入深度
    def __init__(
        self, workspace: Path, *, global_file: Path | None = None,
        max_bytes: int = 32768, max_file_bytes: int = 16384,
        max_files: int = 16, max_depth: int = 4,
    ) -> None:
        self.workspace = workspace.resolve(strict=True)
        self.global_file = global_file
        self.max_bytes = max_bytes
        self.max_file_bytes = max_file_bytes
        self.max_files = max_files
        self.max_depth = max_depth

    # 在启动快照中递归展开受控导入，拒绝越界、环与过大文件
    def load(self, cwd: Path | None = None) -> InstructionSnapshot:
        snapshot = InstructionSnapshot()
        visited: set[Path] = set()
        hashes: set[str] = set()
        total = 0

        # 只读取根目录内的普通文件，导入关系不会改变授权范围
        def visit(path: Path, root: Path, scope: str, depth: int, stack: set[Path]) -> None:
            nonlocal total
            if not path.exists() and not path.is_symlink():
                return
            try:
                resolved = path.resolve(strict=True)
                if resolved.suffix.lower() not in {".md", ".txt"}:
                    raise ValueError("instruction imports must be markdown or text")
                if not resolved.is_relative_to(root) or path.is_symlink():
                    raise ValueError("instruction path escapes allowed root")
                if resolved in stack or depth > self.max_depth:
                    raise ValueError("instruction import cycle or depth limit")
                if resolved in visited:
                    return
                if len(visited) >= self.max_files:
                    raise ValueError("instruction file count exceeded")
                fd = _open_within(root, resolved)
                with os.fdopen(fd, "rb") as stream:
                    metadata = os.fstat(stream.fileno())
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o022:
                        raise ValueError("instruction file is special or writable by others")
                    raw = stream.read(self.max_file_bytes + 1)
                if len(raw) > self.max_file_bytes or total + len(raw) > self.max_bytes:
                    raise ValueError("instruction byte budget exceeded")
                text = raw.decode("utf-8")
                visited.add(resolved)
                total += len(raw)
                digest = hashlib.sha256(raw).hexdigest()
                if digest not in hashes:
                    hashes.add(digest)
                    snapshot.sources.append(InstructionSource(
                        str(resolved), digest, scope, len(snapshot.sources), metadata.st_mtime_ns,
                        str(redact(text)),
                    ))
                for line in text.splitlines():
                    if line.startswith("@import "):
                        target = line.removeprefix("@import ").strip()
                        if not target or Path(target).is_absolute():
                            raise ValueError("instruction imports must be relative")
                        visit(resolved.parent / target, root, scope, depth + 1, stack | {resolved})
            except (OSError, UnicodeError, ValueError) as exc:
                snapshot.diagnostics.append(f"{path.name}: {exc}")

        if self.global_file is not None:
            global_path = self.global_file.expanduser()
            visit(global_path, global_path.parent.resolve(), "global", 0, set())
        for name in ("CLAUDE.md", "AGENTS.md", ".claudedock/context.md", "CLAUDE.local.md"):
            visit(self.workspace / name, self.workspace, "project", 0, set())
        current = (cwd or self.workspace).resolve()
        if not current.is_relative_to(self.workspace):
            raise ValueError("working directory is outside workspace")
        relative = current.relative_to(self.workspace)
        directory = self.workspace
        for part in relative.parts:
            directory /= part
            for name in ("CLAUDE.md", "AGENTS.md"):
                visit(directory / name, self.workspace, "directory", 0, set())
        return snapshot
