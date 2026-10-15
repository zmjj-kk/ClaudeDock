from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

from kama_claude.core.permissions.policy import (
    DEFAULT_POLICIES,
    PermissionDecision,
    ToolPolicy,
    matches_outside_cwd,
    param_preview,
)
from kama_claude.core.permissions.rules import Rule, evaluate_rules, load_rules
from kama_claude.core.permissions.storage import load_policy_file
from kama_claude.core.security.classifier import RiskClassifier
from kama_claude.core.security.context import digest, local_principal
from kama_claude.core.security.decision_audit import SecurityAudit
from kama_claude.core.transport.socket_server import get_connection_writer

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.datetime.now(UTC).isoformat()


@dataclass
class _PendingRequest:
    future: asyncio.Future[str]
    session_id: str
    tool_name: str
    owner: object | None
    approval_token: str = ""


# 管理工具调用权限：策略评估、用户审批挂起、session 级和持久化 always 缓存、超时
class PermissionManager:
    def __init__(
        self,
        policies: dict[str, ToolPolicy] | None = None,
        *,
        policy_file: Path | None = None,
        timeout_s: float = 60.0,
        mode: str = "default",
        bypass_enabled: bool = False,
        principal_id: str | None = None,
        project_root: Path | None = None,
        classifier: RiskClassifier | None = None,
        rules: tuple[Rule, ...] | None = None,
        audit: SecurityAudit | None = None,
    ) -> None:
        if mode not in {"default", "plan", "auto", "bypass"}:
            raise ValueError("invalid permission mode")
        self.audit = audit
        self.mode = mode
        self.bypass_enabled = bypass_enabled
        self.principal_id = principal_id or local_principal()
        self.project_root = (project_root or Path.cwd()).resolve()
        self.rules = (
            rules
            if rules is not None
            else load_rules(
                (
                    (Path("/etc/kama/policy.toml"), "global"),
                    (Path("~/.kama/policy.toml").expanduser(), "user"),
                    (self.project_root / ".kama/policy.toml", "project"),
                )
            )
        )
        self.classifier = classifier
        self._policies: dict[str, ToolPolicy] = (
            dict(DEFAULT_POLICIES) if policies is None else policies
        )
        # tool_use_id → pending Future + metadata
        self._pending: dict[str, _PendingRequest] = {}
        # (session_id, tool_name) → "allow" | "deny"（session 内存，重启丢失）
        self._session_always: dict[tuple[str, str, str], str] = {}
        self._expirations: dict[tuple[str, str, str], float] = {}
        # tool_name → "allow" | "deny"（持久化，从 policy_file 加载）
        self._policy_file = policy_file
        self._persistent_always: dict[str, str] = (
            load_policy_file(policy_file) if policy_file is not None else {}
        )
        # 0 表示不超时
        self._timeout_s = timeout_s

    # 策略版本包含所有实际规则与主体，修改配置后缓存立即失效
    @property
    def policy_revision(self) -> str:
        return digest(
            {
                "rules": [vars(rule) for rule in self.rules],
                "policies": {
                    name: {
                        "default": str(p.default),
                        "allow": p.allow_patterns,
                        "deny": p.deny_patterns,
                    }
                    for name, p in self._policies.items()
                },
                "persistent_deny": sorted(
                    k for k, v in self._persistent_always.items() if v == "deny"
                ),
                "principal": self.principal_id,
                "mode": self.mode,
                "bypass": self.bypass_enabled,
                "root": str(self.project_root),
            }
        )

    # 对工具名 + 参数执行 4 层静态评估，不挂起
    def evaluate(self, tool_name: str, params: dict[str, Any]) -> PermissionDecision:
        from kama_claude.core.permissions.policy import evaluate

        if self._persistent_always.get(tool_name) == "deny":
            return PermissionDecision.DENY
        policy = self._policies.get(tool_name)
        base = evaluate(tool_name, params, policy)
        rule = evaluate_rules(self.rules, tool_name, digest(params))
        if base == PermissionDecision.DENY or rule == PermissionDecision.DENY:
            return PermissionDecision.DENY
        return rule or base

    # 检查权限；如需 ask 则向客户端发事件并等待响应；返回 (allowed, decision_str)
    async def check_and_wait(
        self,
        tool_use_id: str,
        tool_name: str,
        params: dict[str, Any],
        session_id: str,
        event_emitter: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        approval_scope: str = "",
        force_ask: bool = False,
        cacheable: bool = True,
        approval_deadline: float | None = None,
    ) -> tuple[bool, str]:
        params = deepcopy(params)
        revision = self.policy_revision
        binding = digest(
            {"scope": approval_scope, "principal": self.principal_id, "policy": revision}
        )
        rule = evaluate_rules(self.rules, tool_name, digest(params))
        if rule == PermissionDecision.ASK:
            force_ask, cacheable = True, False
        static = self.evaluate(tool_name, params)
        if static == PermissionDecision.DENY:
            return False, "auto_deny"
        command = str(params.get("command", "")) if tool_name == "bash" else ""
        policy = self._policies.get(tool_name)

        # Tier 1: deny_patterns（bash only，不可被缓存绕过）
        if command and policy:
            for pat in policy.deny_patterns:
                if re.search(pat, command):
                    logger.debug("permission: deny_pattern hit tool=%s", tool_name)
                    return False, "auto_deny"

        # Tier 2: OUTSIDE_CWD_HEURISTICS（bash only，强制 ASK，不可被任何缓存绕过）
        outside_cwd = bool(command and matches_outside_cwd(command))

        if not outside_cwd and not force_ask:
            # Tier 3: session always 缓存
            session_key = (
                session_id,
                tool_name,
                self._digest({"params": params, "binding": binding}),
            )
            if self._expirations.get(session_key, 0) <= time.monotonic():
                self._session_always.pop(session_key, None)
                self._expirations.pop(session_key, None)
            if session_key in self._session_always:
                cached = self._session_always[session_key]
                logger.debug("permission: session cache hit tool=%s decision=%s", tool_name, cached)
                return cached == "allow", f"auto_{cached}"

            # Tier 4: persistent always（跨 session）
            if self._persistent_always.get(tool_name) == "deny":
                cached = self._persistent_always[tool_name]
                logger.debug(
                    "permission: persistent cache hit tool=%s decision=%s", tool_name, cached
                )
                return cached == "allow", f"auto_{cached}"

            # Tier 5: allow_patterns（bash only）
            if command and policy:
                for pat in policy.allow_patterns:
                    if re.search(pat, command):
                        return True, "auto_allow"

            # Tier 6: tool default
            if policy is not None:
                if policy.default == PermissionDecision.ALLOW:
                    return True, "auto_allow"
                if policy.default == PermissionDecision.DENY:
                    return False, "auto_deny"
            # default == ASK（bash、unknown tool）→ fall through to Future

        # ASK 路径（来自 OUTSIDE_CWD 强制 ASK，或 default=ASK）
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        if tool_use_id in self._pending:
            raise ValueError("duplicate pending approval id")
        try:
            owner: object | None = get_connection_writer()
        except LookupError:
            owner = None
        approval_token = secrets.token_urlsafe(32) if approval_scope else ""
        self._pending[tool_use_id] = _PendingRequest(
            future=future,
            session_id=session_id,
            tool_name=tool_name,
            owner=owner,
            approval_token=approval_token,
        )

        try:
            await event_emitter(
                {
                    "type": "permission.requested",
                    "tool_use_id": tool_use_id,
                    "tool_name": tool_name,
                    "params": params,
                    "param_preview": param_preview(tool_name, params),
                    "session_id": session_id,
                    "approval_token": approval_token,
                    "approval_scope": approval_scope,
                    "policy_revision": revision,
                    "ts": _now(),
                }
            )
            wait_s = self._timeout_s
            if approval_deadline is not None:
                remaining = max(0.001, approval_deadline - time.monotonic())
                wait_s = min(wait_s, remaining) if wait_s > 0 else remaining
            if wait_s > 0:
                raw = await asyncio.wait_for(future, timeout=wait_s)
            else:
                raw = await future
            if self.policy_revision != revision:
                return False, "policy_changed"
            allowed = self._apply_response(
                raw,
                session_id,
                tool_name,
                params,
                binding=binding,
                cacheable=cacheable,
            )
            return allowed, raw
        except TimeoutError:
            return False, "timeout"
        finally:
            self._pending.pop(tool_use_id, None)
            if not future.done():
                future.cancel()

    # 处理客户端返回的审批决策，resolve 对应 Future
    def respond(
        self,
        tool_use_id: str,
        decision: str,
        *,
        owner: object | None = None,
        approval_token: str = "",
    ) -> None:
        req = self._pending.get(tool_use_id)
        if req is None:
            return
        if req.owner is not owner:
            raise PermissionError("approval belongs to another connection")
        if req.approval_token and not secrets.compare_digest(req.approval_token, approval_token):
            raise PermissionError("approval token mismatch or replay")
        if decision not in {"allow_once", "deny_once", "always_allow", "always_deny"}:
            raise ValueError("invalid approval decision")
        if not req.future.done():
            req.future.set_result(decision)

    # 对参数做规范摘要，任何参数变化都必须重新审批
    @staticmethod
    def _digest(params: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()

    # 永久选项仅缓存当前会话的精确参数，旧工具级允许记录不再生效
    def _apply_response(
        self,
        decision: str,
        session_id: str,
        tool_name: str,
        params: dict[str, Any],
        *,
        binding: str = "",
        cacheable: bool = True,
    ) -> bool:
        if cacheable and decision in {"always_allow", "always_deny"}:
            key = (session_id, tool_name, self._digest({"params": params, "binding": binding}))
            self._expirations[key] = time.monotonic() + 3600
            if len(self._session_always) >= 4096:
                oldest = min(self._expirations, key=lambda k: self._expirations[k])
                self._session_always.pop(oldest, None)
                self._expirations.pop(oldest, None)
            self._session_always[key] = "allow" if decision == "always_allow" else "deny"
        return decision in {"allow_once", "always_allow"}

    # 会话关闭后撤销内存授权，下一次调用必须重新审批
    def revoke_session(self, session_id: str) -> None:
        for key in list(self._session_always):
            if key[0] == session_id:
                self._session_always.pop(key, None)
                self._expirations.pop(key, None)

    # 客户端断开时仅取消属于该连接的审批，防止影响其他客户端
    def cancel_owner(self, owner: object) -> None:
        for req in list(self._pending.values()):
            if req.owner is owner and not req.future.done():
                req.future.set_result("deny_once")

    # 客户端断连时拒绝该 session 所有待审批请求，防止 Future 永久挂起
    def cancel_session(self, session_id: str, reason: str = "client_disconnected") -> None:
        to_cancel = [uid for uid, req in self._pending.items() if req.session_id == session_id]
        for uid in to_cancel:
            req = self._pending.pop(uid)
            if not req.future.done():
                logger.debug("permission: cancel pending tool_use_id=%s reason=%s", uid, reason)
                req.future.set_result("deny_once")
