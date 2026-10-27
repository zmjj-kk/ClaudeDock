from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from claudedock.core.permissions.policy import PermissionDecision, evaluate
from claudedock.core.permissions.rules import evaluate_rules
from claudedock.core.security.capabilities import Capability
from claudedock.core.security.context import SecurityContext, digest
from claudedock.core.security.redaction import redact
from claudedock.core.tools.paths import resolve_inside

if TYPE_CHECKING:
    from claudedock.core.llm.types import ToolCallBlock
    from claudedock.core.permissions.manager import PermissionManager
    from claudedock.core.tools.base import BaseTool


@dataclass(frozen=True)
class Decision:
    action: PermissionDecision
    reason_code: str
    policy_revision: str
    constraints: tuple[str, ...] = ("runtime_boundary",)
    approval_scope: str = ""
    mandatory_approval: bool = False


class SecurityGate:
    # 统一评估不可跳过的规则、工具约束与模式，工具自检不能提升权限
    def evaluate(
        self,
        context: SecurityContext,
        call: ToolCallBlock,
        tool: BaseTool,
        manager: PermissionManager | None = None,
    ) -> Decision:
        scope = digest(
            {
                "principal": context.principal_id,
                "session": context.session_id,
                "project": str(context.project_root),
                "server": context.server_id,
                "tool": tool.security_identity(),
                "arguments": call.input,
                "policy": context.policy_revision,
                "mode": context.mode,
            }
        )

        # 保留机器可审计的原因代码及完整参数作用域摘要
        def result(
            action: PermissionDecision,
            reason: str,
            mandatory: bool = False,
        ) -> Decision:
            return Decision(
                action,
                reason,
                context.policy_revision,
                approval_scope=scope,
                mandatory_approval=mandatory,
            )

        if not context.valid() or call.name != tool.name or call.id == "":
            return result(PermissionDecision.DENY, "invalid_context")
        if context.server_id != tool.server_id:
            return result(PermissionDecision.DENY, "server_identity_mismatch")
        if manager is not None and context.policy_revision != manager.policy_revision:
            return result(PermissionDecision.DENY, "policy_changed")
        static = (
            manager.evaluate(call.name, dict(call.input))
            if manager
            else evaluate(call.name, dict(call.input))
        )
        if static == PermissionDecision.DENY:
            return result(PermissionDecision.DENY, "static_deny")
        rejection = tool.check_permissions(context, dict(call.input))
        if rejection:
            return result(PermissionDecision.DENY, rejection)
        if call.name in {"read_file", "write_file", "list_dir"} and tool.server_id == "builtin":
            safe = resolve_inside(context.project_root, str(call.input.get("path", ".")))
            if safe.path is None:
                return result(PermissionDecision.DENY, "protected_or_outside_path")
        capabilities = tool.capabilities
        pure_read = (
            type(tool).__module__.startswith("claudedock.")
            and capabilities == frozenset({Capability.READ})
            and tool.server_id == "builtin"
        )
        if (
            manager
            and evaluate_rules(manager.rules, call.name, digest(call.input))
            == PermissionDecision.ASK
        ):
            return result(PermissionDecision.ASK, "rule_explicit_approval", True)
        if context.mode == "plan":
            return result(
                PermissionDecision.ALLOW if pure_read else PermissionDecision.DENY,
                "plan_read" if pure_read else "plan_side_effect",
            )
        if context.mode == "bypass" and (manager is None or not manager.bypass_enabled):
            return result(PermissionDecision.DENY, "bypass_disabled")
        # 未授信 MCP 和未知能力不能因描述、模式、模型或 allow 规则获得自动批准
        if tool.server_id != "builtin":
            if not getattr(tool, "trusted_registration", False):
                return result(PermissionDecision.DENY, "mcp_untrusted_registration")
            return result(PermissionDecision.ASK, "mcp_explicit_approval", True)
        if redact(dict(call.input)) != dict(call.input):
            return result(PermissionDecision.ASK, "sensitive_input", True)
        if not capabilities:
            return result(PermissionDecision.ASK, "unknown_capability", True)
        if capabilities & {Capability.EXECUTE, Capability.NETWORK, Capability.EXTERNAL_EFFECT}:
            return result(PermissionDecision.ASK, "effect_explicit_approval", True)
        if pure_read:
            return result(
                static,
                "bounded_read",
            )
        if context.mode == "bypass":
            return result(PermissionDecision.ALLOW, "restricted_bypass")
        if static == PermissionDecision.ALLOW:
            return result(PermissionDecision.ALLOW, "explicit_policy")
        return result(
            PermissionDecision.ASK, "auto_review" if context.mode == "auto" else "approval"
        )

    # 审批等待后重新验证期限，调用参数、工具身份和策略必须仍与评估时一致
    def unchanged(
        self,
        context: SecurityContext,
        call: ToolCallBlock,
        tool: BaseTool,
        expected: Decision,
        manager: PermissionManager | None,
    ) -> bool:
        current = self.evaluate(context, call, tool, manager)
        return (
            context.deadline > time.monotonic()
            and current.action != PermissionDecision.DENY
            and current.approval_scope == expected.approval_scope
        )
