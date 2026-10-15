from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from kama_claude.core.bus.events import (
    PermissionDeniedEvent,
    PermissionGrantedEvent,
    PermissionRequestedEvent,
    SecurityDecisionEvent,
    ToolCallFailedEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
)
from kama_claude.core.compact.tool_result_store import current_result_budget
from kama_claude.core.events.bus import EventBus
from kama_claude.core.journal import current_journal
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.monitoring.telemetry import observe
from kama_claude.core.permissions.policy import PermissionDecision
from kama_claude.core.security.context import SecurityContext, digest, local_principal
from kama_claude.core.security.gate import SecurityGate
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.errors import RateLimitedError
from kama_claude.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from kama_claude.core.permissions.manager import PermissionManager

_DEFAULT_TIMEOUT: float = 120.0
_MAX_RETRIES: int = 2
_RETRY_BASE_S: float = 2.0  # backoff base; tests can monkeypatch to 0
_RETRYABLE: frozenset[str] = frozenset({"runtime_error", "rate_limited"})


# 判断 error_class 是否应重试：须属于可重试全集，且该工具声明此类错误可安全重试
def _should_retry(error_class: str, tool: BaseTool) -> bool:
    return error_class in _RETRYABLE and error_class in tool.retryable_errors


def _now() -> str:
    return datetime.now(UTC).isoformat()


# 发布 ToolCallFailedEvent 并返回对应 ToolResult
async def _fail(
    bus: EventBus,
    run_id: str,
    tool_call: ToolCallBlock,
    error_class: str,
    error_message: str,
    elapsed_ms: int,
    *,
    attempt: int = 1,
) -> ToolResult:
    output_budget = current_result_budget.get()
    if output_budget is not None:
        error_message = output_budget.bound(
            ToolResult(error_message, is_error=True, error_type=error_class),
            tool_call.id,
        ).content
    await bus.publish(
        ToolCallFailedEvent(
            run_id=run_id,
            tool_use_id=tool_call.id,
            tool_name=tool_call.name,
            error_class=error_class,
            error_message=error_message,
            elapsed_ms=elapsed_ms,
            attempt=attempt,
            ts=_now(),
        )
    )
    return ToolResult(content=error_message, is_error=True, error_type=error_class)


# 为工具完整生命周期关联运行、工具 ID 与操作 ID，不导出敏感参数
async def invoke_tool(
    registry: ToolRegistry,
    tool_call: ToolCallBlock,
    bus: EventBus,
    run_id: str,
    timeout: float = _DEFAULT_TIMEOUT,
    *,
    permission_manager: PermissionManager | None = None,
    session_id: str = "",
) -> ToolResult:
    tool_call = deepcopy(tool_call)
    attributes = {
        "run_id": run_id,
        "session_id": session_id,
        "tool_name": tool_call.name,
        "tool_use_id": tool_call.id,
        "operation_id": f"{run_id}:{tool_call.id}",
    }
    with observe("tool.invoke", attributes):
        result = await _invoke_tool(
            registry,
            tool_call,
            bus,
            run_id,
            timeout,
            permission_manager=permission_manager,
            session_id=session_id,
        )
        attributes["outcome"] = "failed" if result.is_error else "success"
        if permission_manager is not None and permission_manager.audit is not None:
            try:
                permission_manager.audit.record(
                    {
                        **attributes,
                        "type": "security.execution",
                        "argument_hash": digest(tool_call.input),
                        "error_type": result.error_type,
                    }
                )
            except (OSError, ValueError):
                if not result.is_error:
                    return ToolResult(
                        "Execution finished but audit failed; verify resource state before retry.",
                        is_error=True,
                        error_type="unknown_outcome",
                    )
        return result


# 校验参数、检查权限、限时调用工具、发布进度事件，失败时指数退避重试，返回 ToolResult（不抛异常）
async def _invoke_tool(
    registry: ToolRegistry,
    tool_call: ToolCallBlock,
    bus: EventBus,
    run_id: str,
    timeout: float = _DEFAULT_TIMEOUT,
    *,
    permission_manager: PermissionManager | None = None,
    session_id: str = "",
) -> ToolResult:
    t0 = time.monotonic()

    await bus.publish(
        ToolCallStartedEvent(
            run_id=run_id,
            tool_use_id=tool_call.id,
            tool_name=tool_call.name,
            params=dict(tool_call.input),
            ts=_now(),
        )
    )

    def elapsed() -> int:
        return int((time.monotonic() - t0) * 1000)

    tool = registry.get(tool_call.name)
    if tool is None:
        return await _fail(
            bus,
            run_id,
            tool_call,
            "runtime_error",
            f"unknown tool: {tool_call.name}",
            elapsed(),
        )

    try:
        digest(tool_call.input)
        registry.validate(tool_call.name, dict(tool_call.input))
    except (ValueError, TypeError, RecursionError) as exc:
        return await _fail(bus, run_id, tool_call, "schema_error", str(exc), elapsed())

    if tool.params_model is not None:
        try:
            tool.params_model.model_validate(dict(tool_call.input))
        except ValidationError as exc:
            return await _fail(
                bus,
                run_id,
                tool_call,
                "schema_error",
                str(exc),
                elapsed(),
            )

    manager = permission_manager
    context = SecurityContext(
        principal_id=manager.principal_id if manager else local_principal(),
        project_root=manager.project_root if manager else Path.cwd().resolve(),
        session_id=session_id or f"run:{run_id}",
        run_id=run_id,
        operation_id=f"{run_id}:{tool_call.id}",
        server_id=tool.server_id,
        policy_revision=manager.policy_revision if manager else "standalone",
        mode=manager.mode if manager else "default",
        deadline=time.monotonic() + timeout,
    )
    gate = SecurityGate()
    try:
        gate_decision = gate.evaluate(context, tool_call, tool, manager)
    except Exception:
        return await _fail(
            bus,
            run_id,
            tool_call,
            "permission_denied",
            "Security evaluation failed closed",
            elapsed(),
        )
    action, reason = gate_decision.action, gate_decision.reason_code
    if (
        action == PermissionDecision.ASK
        and reason == "auto_review"
        and manager is not None
        and manager.classifier is not None
    ):
        classified = await manager.classifier.classify(
            {
                "scope": gate_decision.approval_scope,
                "tool_identity": tool.security_identity(),
                "arguments": tool_call.input,
                "capabilities": sorted(tool.capabilities),
                "policy_revision": context.policy_revision,
            }
        )
        action, reason = classified.action, classified.reason_code
    await bus.publish(
        SecurityDecisionEvent(
            run_id=run_id,
            session_id=session_id,
            tool_use_id=tool_call.id,
            operation_id=context.operation_id,
            principal_id=context.principal_id,
            server_id=context.server_id,
            argument_hash=digest(tool_call.input),
            policy_revision=context.policy_revision,
            action=str(action),
            reason_code=reason,
            elapsed_ms=elapsed(),
            ts=_now(),
        )
    )
    if manager is not None and manager.audit is not None:
        try:
            manager.audit.record(
                {
                    "type": "security.decision",
                    "operation_id": context.operation_id,
                    "principal": context.principal_id,
                    "session": session_id,
                    "run": run_id,
                    "server": context.server_id,
                    "tool": tool.security_identity(),
                    "argument_hash": digest(tool_call.input),
                    "policy": context.policy_revision,
                    "action": str(action),
                    "reason": reason,
                    "elapsed_ms": elapsed(),
                }
            )
        except (OSError, ValueError):
            return await _fail(
                bus,
                run_id,
                tool_call,
                "permission_denied",
                "Security audit unavailable",
                elapsed(),
            )
    if action == PermissionDecision.DENY or (action == PermissionDecision.ASK and manager is None):
        return await _fail(
            bus,
            run_id,
            tool_call,
            "permission_denied",
            f"Security gate: {reason}",
            elapsed(),
        )

    journal = current_journal.get()
    if journal is not None:
        previous = journal.prepare(tool_call.id, tool_call.name, dict(tool_call.input))
        if previous is not None:
            if previous["state"] == "completed":
                return ToolResult(content=previous.get("output", ""))
            return await _fail(
                bus,
                run_id,
                tool_call,
                "unknown_outcome",
                "Previous operation outcome is unknown; verify resource state "
                "before issuing a new operation",
                elapsed(),
            )

    if permission_manager is not None and action == PermissionDecision.ASK:

        async def _emit_permission(raw: dict[str, Any]) -> None:
            await bus.publish(PermissionRequestedEvent(**raw, run_id=run_id))

        if journal is not None:
            journal.data["state"] = "waiting_approval"
            journal.save()
        allowed, decision = await permission_manager.check_and_wait(
            tool_use_id=tool_call.id,
            tool_name=tool_call.name,
            params=dict(tool_call.input),
            session_id=session_id,
            event_emitter=_emit_permission,
            approval_scope=gate_decision.approval_scope,
            force_ask=gate_decision.mandatory_approval,
            cacheable=not gate_decision.mandatory_approval,
            approval_deadline=context.deadline,
        )
        if journal is not None:
            journal.data["state"] = "running"
            journal.save()
        if allowed:
            if decision not in ("auto_allow",):
                await bus.publish(
                    PermissionGrantedEvent(
                        run_id=run_id,
                        tool_use_id=tool_call.id,
                        decision=decision,
                        ts=_now(),
                    )
                )
        else:
            if decision != "auto_deny":
                await bus.publish(
                    PermissionDeniedEvent(
                        run_id=run_id,
                        tool_use_id=tool_call.id,
                        decision=decision,
                        ts=_now(),
                    )
                )
            return await _fail(
                bus,
                run_id,
                tool_call,
                "permission_denied",
                "Permission denied by user. You may not execute this command. "
                "Try an alternative approach or ask the user what to do.",
                elapsed(),
            )

    if manager is not None and manager.audit is not None:
        try:
            manager.audit.record(
                {
                    "type": "security.authorized",
                    "operation_id": context.operation_id,
                    "scope": gate_decision.approval_scope,
                    "policy": context.policy_revision,
                    "principal": context.principal_id,
                }
            )
        except (OSError, ValueError):
            return await _fail(
                bus,
                run_id,
                tool_call,
                "permission_denied",
                "Security audit unavailable",
                elapsed(),
            )
    if not gate.unchanged(context, tool_call, tool, gate_decision, manager):
        return await _fail(
            bus,
            run_id,
            tool_call,
            "permission_denied",
            "Approval scope changed or expired",
            elapsed(),
        )

    for attempt in range(1, _MAX_RETRIES + 2):
        try:
            registry.validate(tool_call.name, dict(tool_call.input))
        except ValueError:
            return await _fail(
                bus,
                run_id,
                tool_call,
                "permission_denied",
                "Tool identity changed",
                elapsed(),
            )
        if not gate.unchanged(context, tool_call, tool, gate_decision, manager):
            return await _fail(
                bus,
                run_id,
                tool_call,
                "permission_denied",
                "Security context expired",
                elapsed(),
            )
        error_class: str | None = None
        error_message: str | None = None

        try:
            if journal is not None:
                journal.update(tool_call.id, "executing")
            result = await asyncio.wait_for(tool.invoke(dict(tool_call.input)), timeout=timeout)
            ms = elapsed()
            output_budget = current_result_budget.get()
            executed_successfully = not result.is_error
            if output_budget is not None:
                result = output_budget.bound(result, tool_call.id)
            if executed_successfully and result.error_type == "output_storage_error":
                if journal is not None:
                    journal.update(tool_call.id, "completed", output=result.content)
                return result

            if result.is_error:
                error_class = result.error_type or "runtime_error"
                error_message = result.content
            else:
                if journal is not None:
                    journal.update(tool_call.id, "completed", output=result.content)
                await bus.publish(
                    ToolCallFinishedEvent(
                        run_id=run_id,
                        tool_use_id=tool_call.id,
                        tool_name=tool_call.name,
                        elapsed_ms=ms,
                        output=result.content,
                        ts=_now(),
                    )
                )
                return result

        except asyncio.CancelledError:
            if journal is not None:
                journal.update(tool_call.id, "unknown")
            raise
        except RateLimitedError as exc:
            error_class = "rate_limited"
            error_message = str(exc)
        except TimeoutError:
            if journal is not None:
                journal.update(tool_call.id, "unknown")
            return await _fail(
                bus,
                run_id,
                tool_call,
                "timeout",
                f"tool timed out after {timeout}s",
                elapsed(),
                attempt=attempt,
            )
        except Exception as exc:
            error_class = "runtime_error"
            error_message = str(exc)

        if journal is not None:
            journal.update(tool_call.id, "failed")
        assert error_class is not None and error_message is not None
        ms = elapsed()

        if _should_retry(error_class, tool) and attempt <= _MAX_RETRIES:
            await bus.publish(
                ToolCallFailedEvent(
                    run_id=run_id,
                    tool_use_id=tool_call.id,
                    tool_name=tool_call.name,
                    error_class=error_class,
                    error_message=error_message,
                    elapsed_ms=ms,
                    attempt=attempt,
                    ts=_now(),
                )
            )
            await asyncio.sleep(_RETRY_BASE_S * (2 ** (attempt - 1)))
            continue

        return await _fail(
            bus,
            run_id,
            tool_call,
            error_class,
            error_message,
            ms,
            attempt=attempt,
        )

    # unreachable, but keeps mypy happy
    return ToolResult(content="internal error", is_error=True, error_type="runtime_error")
