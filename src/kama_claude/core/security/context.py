from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# 对完整规范参数生成摘要，拒绝非有限数字与非 JSON 数据
def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class SecurityContext:
    principal_id: str
    project_root: Path
    session_id: str
    run_id: str
    operation_id: str
    trust_origin: str = "agent"
    server_id: str = "builtin"
    policy_revision: str = ""
    mode: str = "default"
    deadline: float = 0.0

    # 校验运行身份、有效期和工作区；外部文本不能作为调用主体
    def valid(self) -> bool:
        return bool(
            self.principal_id
            and self.session_id
            and self.run_id
            and self.operation_id
            and self.trust_origin in {"agent", "user", "subagent"}
            and self.mode in {"plan", "default", "auto", "bypass"}
            and self.project_root.is_absolute()
            and self.project_root != Path(self.project_root.anchor)
            and self.deadline > time.monotonic()
            and self.policy_revision
        )


# 本地 daemon 的鉴权边界是当前操作系统用户，IPC 连接另行绑定审批归属
def local_principal() -> str:
    return f"local:{os.getuid()}" if hasattr(os, "getuid") else f"local:{os.getlogin()}"
