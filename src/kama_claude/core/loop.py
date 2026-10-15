from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from kama_claude.core.bus.events import (
    ContextBudgetCheckedEvent,
    ContextPrunedEvent,
    StepFinishedEvent,
    StepStartedEvent,
)
from kama_claude.core.compact.microcompact import microcompact, validate_messages
from kama_claude.core.compact.token_budget import check_budget, provider_capacity
from kama_claude.core.compact.tool_result_store import (
    ResultBudget,
    ToolResultStore,
    current_result_budget,
)
from kama_claude.core.config import CompactionConfig
from kama_claude.core.context import ExecutionBudget, ExecutionContext, current_budget
from kama_claude.core.events.bus import EventBus
from kama_claude.core.journal import current_journal
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.llm.context_errors import ContextWindowExceeded
from kama_claude.core.llm.governed import BudgetExceeded
from kama_claude.core.monitoring.telemetry import observe
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from kama_claude.core.compact.compactor import Compactor
    from kama_claude.core.permissions.manager import PermissionManager


log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class AgentLoop:
    # 初始化循环依赖：LLM provider、工具注册表、事件总线，以及可选的权限管理器、压缩器、session ID
    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        bus: EventBus,
        *,
        permission_manager: PermissionManager | None = None,
        compactor: Compactor | None = None,
        compact_threshold: float = 0.80,
        session_id: str = "",
        max_duration_s: float = 1800.0,
        max_tool_calls: int = 256,
        max_tokens: int = 1_000_000,
        stagnation_limit: int = 3,
        tool_slots: asyncio.Semaphore | None = None,
        compaction_config: CompactionConfig | None = None,
        result_store: ToolResultStore | None = None,
    ) -> None:
        self._config = compaction_config or CompactionConfig()
        self._config.validate()
        self._result_store = result_store
        self._provider = provider
        self._registry = registry
        self._bus = bus
        self._permission_manager = permission_manager
        self._compactor = compactor
        self._compact_threshold = compact_threshold
        self._session_id = session_id
        self._max_duration_s = max_duration_s
        self._max_tool_calls = max_tool_calls
        self._max_tokens = max_tokens
        self._stagnation_limit = stagnation_limit
        self._tool_slots = tool_slots or asyncio.Semaphore(16)

    # 驱动 plan→act→observe 循环直到上下文终止；CancelledError 向上传播
    async def run(self, context: ExecutionContext) -> None:
        attributes = {"run_id": context.run_id, "parent_run_id": context.parent_run_id or ""}
        with observe("agent.run", attributes):
            await self._run(context)
            attributes["outcome"] = "success" if context.status == "success" else "failed"

    # 在运行时间预算内启动循环并恢复上下文变量
    async def _run(self, context: ExecutionContext) -> None:
        token = current_budget.set(context.budget)
        result_token = current_result_budget.set(
            ResultBudget(self._result_store, context.run_id, self._config.tool_round_max_bytes)
            if self._result_store is not None else None
        )
        try:
            if context.budget is None:
                context.budget = ExecutionBudget(
                    max_tokens=self._max_tokens,
                    max_tool_calls=self._max_tool_calls,
                    deadline=time.monotonic() + self._max_duration_s,
                )
            current_budget.set(context.budget)
            async with asyncio.timeout(
                min(self._max_duration_s, max(0, context.budget.deadline - time.monotonic()))
            ):
                await self._run_budgeted(context)
        except TimeoutError:
            context.mark_failed("duration_budget_exhausted")
        finally:
            current_budget.reset(token)
            current_result_budget.reset(result_token)

    # 限制累计令牌与工具调用，对重复工具输入强制重新规划后停止
    async def _run_budgeted(self, context: ExecutionContext) -> None:
        budget = context.budget
        assert budget is not None
        signatures: dict[str, int] = {}
        reactive_retries = 0
        while not context.is_done():
            try:
                if not await self._preflight(context):
                    break
            except BudgetExceeded as exc:
                context.mark_failed(str(exc))
                break
            except (OSError, ValueError):
                context.mark_failed("context_persistence_error")
                break
            journal = current_journal.get()
            if journal is not None:
                journal.data["audit_messages"] = context.audit_messages
                journal.data.setdefault("audit_start", len(context.prefill_messages) or 1)
                journal.checkpoint(context.messages)
            if budget.model_calls >= budget.max_model_calls:
                context.mark_failed("model_call_budget_exhausted")
                break
            if getattr(self._provider, "governed", False) is not True:
                budget.model_calls += 1
            context.step += 1
            await self._bus.publish(
                StepStartedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )

            # [plan] call LLM — API errors terminate the run
            try:
                response = await self._provider.chat(
                    messages=context.model_messages(),
                    tool_schemas=self._registry.tool_schemas(),
                    bus=self._bus,
                    run_id=context.run_id,
                    step=context.step,
                    system=context.system_prompt(
                        "You are a helpful AI assistant. "
                        "Use the available tools to complete the user's goal. "
                        "When the goal is fully achieved, respond with a final answer "
                        "and do not call any more tools."
                    ),
                )
            except ContextWindowExceeded:
                if (
                    self._config.enabled and self._compactor is not None
                    and reactive_retries < self._config.reactive_max_retries
                ):
                    reactive_retries += 1
                    try:
                        recovery = await self._compactor.compact(
                            context, self._provider, hard_limit=True,
                        )
                    except BudgetExceeded as exc:
                        context.mark_failed(str(exc))
                        break
                    if recovery is not None:
                        continue
                context.mark_failed("context_too_large")
                break
            except BudgetExceeded as exc:
                context.mark_failed(str(exc))
                break
            except asyncio.CancelledError:
                context.mark_failed("cancelled")
                raise
            except Exception:
                logging.getLogger(__name__).exception(
                    "LLM call failed run_id=%s step=%d", context.run_id, context.step
                )
                context.mark_failed("llm_error")
                break

            if response.usage is not None and not response.usage.accounted:
                budget.tokens += (
                    response.usage.input_tokens
                    + response.usage.output_tokens
                    + response.usage.cache_read_input_tokens
                    + response.usage.cache_creation_input_tokens
                )
                if budget.tokens > budget.max_tokens:
                    context.mark_failed("token_budget_exhausted")
                    break

            reactive_retries = 0
            result_budget = current_result_budget.get()
            if result_budget is not None:
                result_budget.remaining = self._config.tool_round_max_bytes
            # [observe] append assistant content blocks to context
            # thinking blocks must come first and be preserved verbatim for extended thinking mode
            blocks: list[dict[str, object]] = list(response.thinking_blocks)
            if response.text:
                blocks.append({"type": "text", "text": response.text})
            for tc in response.tool_calls:
                blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input})
            context.add_assistant_message(blocks)

            # [act] execute each requested tool; errors become tool results so loop continues
            if response.stop_reason == "tool_use":
                for tc in response.tool_calls:
                    budget.tool_calls += 1
                    signature = tc.name + json.dumps(tc.input, sort_keys=True)
                    signatures[signature] = signatures.get(signature, 0) + 1
                    if budget.tool_calls > budget.max_tool_calls:
                        context.add_tool_result(tc.id, "Tool call budget exhausted", is_error=True)
                        context.mark_failed("tool_budget_exhausted")
                        continue
                    if signatures[signature] >= self._stagnation_limit:
                        context.add_tool_result(
                            tc.id, "Repeated tool input: replan before retrying", is_error=True
                        )
                        if signatures[signature] > self._stagnation_limit:
                            context.mark_failed("no_progress")
                        continue
                    if tc.name == "spawn_agent":
                        result = await invoke_tool(
                            self._registry,
                            tc,
                            self._bus,
                            context.run_id,
                            permission_manager=self._permission_manager,
                            session_id=self._session_id,
                        )
                    else:
                        async with self._tool_slots:
                            result = await invoke_tool(
                                self._registry,
                                tc,
                                self._bus,
                                context.run_id,
                                permission_manager=self._permission_manager,
                                session_id=self._session_id,
                            )
                    context.add_tool_result(tc.id, result.content, is_error=result.is_error)
            elif response.stop_reason == "max_tokens" and response.tool_calls:
                # Output token limit hit mid-tool-call; input is incomplete.
                # Add synthetic error results so the conversation stays balanced.
                for tc in response.tool_calls:
                    context.add_tool_result(
                        tc.id,
                        "Error: output token limit reached before this tool call could be "
                        "completed. Please break the task into smaller steps and try again.",
                        is_error=True,
                    )

            # 终止判断：覆盖全部 stop_reason，保证 run 必定收敛，避免截断或未识别原因导致死循环
            if response.stop_reason == "end_turn":
                context.result = response.text or ""
                context.mark_success()
            elif response.stop_reason == "max_tokens" and not response.tool_calls:
                # 纯文本输出被 token 上限截断：直接收尾，避免以 assistant 收尾消息重发导致 API 报错
                context.result = response.text or ""
                context.mark_failed("truncated")
            elif response.stop_reason == "refusal":
                context.result = response.text or ""
                context.mark_failed("refusal")
            elif response.stop_reason not in ("tool_use", "max_tokens"):
                # 未识别的 stop_reason：标记失败终止，防止无限循环
                context.mark_failed(f"unhandled_stop_reason:{response.stop_reason}")
            elif context.step >= context.max_steps:
                context.mark_failed("exceeded_max_steps")

            # 工具结果追加完毕（messages 末尾为 user）后检查压缩，仅在 run 继续时触发
            # 此时压缩结果 [user_summary, assistant_ack] 对下一次 LLM 调用是合法输入
            if (
                not context.is_done()
                and response.stop_reason == "tool_use"
                and self._compactor is not None
                and self._compact_threshold > 0
                and response.usage is not None
                and response.usage.context_pct >= self._compact_threshold
            ):
                try:
                    await self._compactor.compact(context, self._provider)
                except BudgetExceeded as exc:
                    context.mark_failed(str(exc))

            await self._bus.publish(
                StepFinishedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )

    # 发送前预算检查，确定性清理后重算，仍超限则有限失败
    async def _preflight(self, context: ExecutionContext) -> bool:
        config = self._config
        capacity = provider_capacity(self._provider, config.model_context_window)
        system = context.system_prompt(
            "You are a helpful AI assistant. Use the available tools to complete the user's goal. "
            "When the goal is fully achieved, respond with a final answer "
            "and do not call any more tools."
        )
        tools = self._registry.tool_schemas()
        report = check_budget(
            context.model_messages(), tools, system, capacity, config.safety_margin_ratio,
        )
        await self._bus.publish(ContextBudgetCheckedEvent(
            run_id=context.run_id, session_id=self._session_id, window=report.window,
            input_tokens=report.input_tokens, reserved_output=report.reserved_output,
            safety_margin=report.safety_margin, system_tokens=report.system_tokens,
            tools_tokens=report.tools_tokens, history_tokens=report.history_tokens,
            over_limit=report.over_limit, mode=config.preflight_mode,
            estimate_method=report.estimate_method, ts=_now(),
        ))
        if config.preflight_mode == "shadow":
            return True
        validate_messages(context.messages)
        if report.window is None:
            context.mark_failed("context_window_unknown")
            return False
        high = report.over_limit or (report.ratio or 0) >= config.high_watermark
        if not high:
            return True
        if config.microcompact_enabled:
            candidate, count = microcompact(context.messages, config.recent_turns_min)
            if count:
                if self._compactor is not None:
                    self._compactor.commit_projection(candidate)
                context.compaction_version += 1
                context.messages = candidate
                after = check_budget(
                    candidate, tools, system, capacity, config.safety_margin_ratio,
                )
                await self._bus.publish(ContextPrunedEvent(
                    run_id=context.run_id, count=count,
                    saved_tokens=max(0, report.input_tokens - after.input_tokens), ts=_now(),
                ))
                report = after
        if (
            config.enabled and self._compactor is not None
            and (report.over_limit or (report.ratio or 0) > config.low_watermark)
        ):
            await self._compactor.compact(
                context, self._provider, hard_limit=report.over_limit,
            )
            report = check_budget(
                context.messages, tools, system, capacity, config.safety_margin_ratio,
            )
        if report.over_limit:
            context.mark_failed("context_too_large")
            return False
        return True
