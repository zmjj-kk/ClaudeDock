from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path


# 按端口定位本地凭证文件，远程客户端应显式提供环境变量
def token_path(port: int) -> Path:
    return Path(f"~/.claudedock/ipc-{port}.token").expanduser()


# 读取仅当前用户可访问的认证凭证，拒绝符号链接及宽松权限
def read_token(port: int) -> str:
    configured = os.environ.get("CLAUDEDOCK_IPC_TOKEN")
    if configured:
        return configured
    path = token_path(port)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError("IPC token must be owned by this user with mode 0600")
        token = stream.read(4096).strip()
        if len(token) < 32 or len(token) > 512:
            raise ValueError("IPC token must contain 32 to 512 characters")
        return token


# 首次启动独占创建随机凭证，后续启动复用已有用户凭证
def server_token(port: int) -> str:
    if os.environ.get("CLAUDEDOCK_IPC_TOKEN"):
        token = read_token(port)
        if not 32 <= len(token) <= 512:
            raise ValueError("IPC token must contain 32 to 512 characters")
        return token
    path = token_path(port)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return read_token(port)
    token = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(token)
        stream.flush()
        os.fsync(stream.fileno())
    return token
