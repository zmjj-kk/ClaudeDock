from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from kama_claude.core.context import current_budget
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse
from kama_claude.core.monitoring.telemetry import observe

if TYPE_CHECKING:
    from kama_claude.core.llm.base import LLMProvider


class BudgetExceeded(RuntimeError):
    pass


class GovernedProvider:
    governed = True

    # 所有父子运行共用守护进程的模型并发槽
    def __init__(
        self, inner: LLMProvider, slots: asyncio.Semaphore, prices: dict[str, float] | None = None
    ) -> None:
        self._prices = prices
        self._inner = inner
        self._slots = slots

    # 关联模型请求、运行和步骤，并记录真实请求时长
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        with observe("llm.chat", {"run_id": run_id, "step": step}):
            return await self._chat(messages, tool_schemas, bus, run_id, step=step, system=system)

    # 将模型请求排队到有界并发槽，取消会传播到等待与请求
    async def _chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        budget = current_budget.get()
        async with self._slots:
            if budget is not None:
                if budget.model_calls >= budget.max_model_calls:
                    raise BudgetExceeded("model_call_budget_exhausted")
                if budget.max_cost_usd > 0 and self._prices is None:
                    raise BudgetExceeded("cost_pricing_unavailable")
                if budget.max_cost_usd > 0 and budget.estimated_cost >= budget.max_cost_usd:
                    raise BudgetExceeded("cost_budget_exhausted")
                budget.model_calls += 1
            try:
                response = await self._inner.chat(
                    messages=messages,
                    tool_schemas=tool_schemas,
                    bus=bus,
                    run_id=run_id,
                    step=step,
                    system=system,
                )
            except BaseException:
                if budget is not None:
                    budget.cost_known = False
                raise
            usage = response.usage
            if usage is not None:
                if self._prices is not None:
                    usage.estimated_cost_usd = (
                        usage.input_tokens * self._prices["input"]
                        + usage.output_tokens * self._prices["output"]
                        + usage.cache_read_input_tokens * self._prices["cache_read"]
                        + usage.cache_creation_input_tokens * self._prices["cache_create"]
                    ) / 1_000_000
                if budget is not None:
                    usage.accounted = True
                    budget.tokens += (
                        usage.input_tokens
                        + usage.output_tokens
                        + usage.cache_read_input_tokens
                        + usage.cache_creation_input_tokens
                    )
                    if usage.estimated_cost_usd is None:
                        budget.cost_known = False
                    else:
                        budget.estimated_cost += usage.estimated_cost_usd
                    if budget.tokens > budget.max_tokens:
                        raise BudgetExceeded("token_budget_exhausted")
                    if budget.max_cost_usd > 0 and budget.estimated_cost > budget.max_cost_usd:
                        raise BudgetExceeded("cost_budget_exhausted")
            return response
