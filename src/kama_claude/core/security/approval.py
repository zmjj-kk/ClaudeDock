from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from kama_claude.core.security.context import digest


@dataclass(frozen=True)
class ApprovalScope:
    principal_id: str
    session_id: str
    operation_id: str
    server_id: str
    tool_identity: str
    argument_hash: str
    effect_summary_hash: str
    policy_revision: str


class ApprovalTokens:
    # 内存 token 只用于本地审批一次性消费，不作为外部业务后端授权
    def __init__(self) -> None:
        self._tokens: dict[str, tuple[str, float]] = {}

    # 绑定不可变审批范围并设置有界有效期
    def issue(self, scope: ApprovalScope, ttl_s: float = 60) -> str:
        if not 0 < ttl_s <= 3600:
            raise ValueError("invalid approval TTL")
        now = time.monotonic()
        self._tokens = {k: v for k, v in self._tokens.items() if v[1] > now}
        if len(self._tokens) >= 4096:
            raise PermissionError("approval quota exceeded")
        token = secrets.token_urlsafe(32)
        self._tokens[token] = (digest(vars(scope)), now + ttl_s)
        return token

    # 无论匹配与否一次性消费 token，拒绝跨主体、跨参数和跨服务端重放
    def consume(self, token: str, scope: ApprovalScope) -> bool:
        entry = self._tokens.pop(token, None)
        return bool(
            entry
            and entry[1] > time.monotonic()
            and secrets.compare_digest(entry[0], digest(vars(scope)))
        )
