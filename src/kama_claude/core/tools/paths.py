from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SafePath:
    # 解析后的绝对路径；越界或无法解析时为 None
    path: Path | None
    # 越界/非法路径时的描述信息；成功时为 None
    error: str | None = None


# 将用户路径解析并约束在 root 目录内：展开 ~、消除 .. 与符号链接后拒绝越界
def resolve_inside(root: Path, path_str: str) -> SafePath:
    root = root.expanduser().resolve()
    if root == Path(root.anchor):
        return SafePath(None, "filesystem root cannot be a tool workspace")
    if "\x00" in path_str:
        return SafePath(None, "invalid path")
    raw = Path(path_str).expanduser()
    if not raw.is_absolute():
        raw = root / raw
    try:
        resolved = raw.resolve()
    except (OSError, RuntimeError):
        return SafePath(None, f"invalid path: {path_str}")
    if not resolved.is_relative_to(root):
        return SafePath(None, f"path escapes working directory: {path_str}")
    relative = resolved.relative_to(root)
    lexical_parts = raw.relative_to(root).parts if raw.is_relative_to(root) else ()
    sensitive = {
        ".env",
        ".ssh",
        ".aws",
        ".kube",
        ".git",
        ".kama",
        ".codex",
        ".claude",
        "policy.toml",
        "AGENTS.md",
        "CLAUDE.md",
        "AGENT.md",
    }
    if any(
        part in sensitive
        or part.startswith(".env.")
        or part.endswith((".key", ".pem"))
        or (part.startswith("ipc-") and part.endswith(".token"))
        for part in (*relative.parts, *lexical_parts)
    ):
        return SafePath(None, "sensitive path rejected")
    if resolved.is_file() and resolved.stat().st_nlink > 1:
        return SafePath(None, "hard-linked file rejected")
    return SafePath(resolved)


# 相对当前工作目录解析并约束路径的便捷封装
def safe_path(path_str: str) -> SafePath:
    return resolve_inside(Path.cwd(), path_str)


# 使用根目录描述符逐段打开目录，禁止校验后的符号链接置换
@contextmanager
def parent_descriptor(root: Path, path: Path, *, create: bool = False) -> Iterator[tuple[int, str]]:
    root = root.resolve()
    relative = path.relative_to(root)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, relative.name
    finally:
        os.close(fd)


# 从受控目录描述符读取普通文件，限制大小并拒绝最终符号链接
def read_inside(root: Path, path: Path, limit: int) -> bytes:
    with parent_descriptor(root, path) as (directory, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            import stat

            if (
                not stat.S_ISREG(os.fstat(stream.fileno()).st_mode)
                or os.fstat(stream.fileno()).st_nlink > 1
            ):
                raise PermissionError("only regular files can be read")
            return stream.read(limit)


# 在受控目录中写临时文件并用描述符原子提交，抵御父目录置换竞态
def write_inside(root: Path, path: Path, content: str) -> None:
    with parent_descriptor(root, path, create=True) as (directory, name):
        temporary = f".kama-write-{secrets.token_hex(16)}"
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


# 逐段打开目录并返回受控描述符，枚举子目录时同样拒绝符号链接竞态
@contextmanager
def directory_descriptor(root: Path, path: Path) -> Iterator[int]:
    root = root.resolve()
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.relative_to(root).parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)
