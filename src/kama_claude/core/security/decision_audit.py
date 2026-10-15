from __future__ import annotations

import fcntl
import json
import os
import stat
from pathlib import Path
from typing import Any

from kama_claude.core.security.context import digest
from kama_claude.core.security.redaction import redact


class SecurityAudit:
    # 仅保存安全事件元数据和摘要，追加写并通过哈希链关联轮转文件
    def __init__(self, path: Path, max_bytes: int = 2 * 1024 * 1024) -> None:
        self.path = path
        self.max_bytes = max_bytes
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    # 锁内读取链尾、必要时轮转并追加完整记录；写失败交由调用门 fail closed
    def record(self, payload: dict[str, Any]) -> None:
        if any(parent.is_symlink() for parent in self.path.parents) or self.path.is_symlink():
            raise PermissionError("audit path cannot be a symlink")
        lock_fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(lock_fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            previous = "0" * 64
            if self.path.exists():
                fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "r") as reader:
                    metadata = os.fstat(reader.fileno())
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                        raise PermissionError("audit must be a regular private file")
                    lines = reader.readlines()
                if lines:
                    last = json.loads(lines[-1])
                    previous = last.pop("hash")
                    if digest(last) != previous:
                        raise PermissionError("audit tail integrity failure")
                if self.path.stat().st_size >= self.max_bytes:
                    os.replace(self.path, str(self.path) + ".1")
            entry = {"previous_hash": previous, "event": redact(payload)}
            entry["hash"] = digest(entry)
            fd = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(fd, "a") as writer:
                os.fchmod(writer.fileno(), 0o600)
                writer.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
                writer.flush()
                os.fsync(writer.fileno())
