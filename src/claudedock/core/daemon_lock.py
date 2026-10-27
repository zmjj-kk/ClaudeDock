from __future__ import annotations

import fcntl
import os
from pathlib import Path


class DaemonLock:
    # 一个会话根目录只能由一个 daemon 管理，避免不同端口并发恢复同一历史
    def __init__(self, root: Path) -> None:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._fd = os.open(root / ".daemon.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self._fd)
            raise RuntimeError("another daemon owns this session directory") from exc

    # 进程关闭时释放目录锁，进程崩溃时内核会自动释放
    def close(self) -> None:
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
