from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime

from claudedock.core.bus.events import (
    ToolBatchFinishedEvent,
    ToolBatchPlannedEvent,
    ToolBatchStartedEvent,
)
from claudedock.core.compact.tool_result_store import current_result_budget
from claudedock.core.config import ToolExecutionConfig
from claudedock.core.events.bus import EventBus
from claudedock.core.journal import current_journal
from claudedock.core.llm.types import ToolCallBlock
from claudedock.core.monitoring.metrics import get_collector
from claudedock.core.tools.base import ToolResult
from claudedock.core.tools.concurrency import (
    ExecutionStats,
    ResourceClaim,
    current_execution_stats,
    file_claim,
)
from claudedock.core.tools.registry import ToolRegistry

InvokeOne = Callable[[ToolCallBlock, tuple[asyncio.Semaphore, ...]], Awaitable[ToolResult]]


# 在创建任务前拒绝无法配对的工具协议，整批均不执行
def validate_call_ids(calls: list[ToolCallBlock]) -> bool:
    ids = [call.id for call in calls]
    return all(isinstance(value, str) and bool(value.strip()) for value in ids) and len(
        set(ids)
    ) == len(ids)


class ToolExecutionScheduler:
    # 持有单运行限流器，所有运行共享宿主传入的全局执行槽
    def __init__(
        self,
        registry: ToolRegistry,
        invoke_one: InvokeOne,
        bus: EventBus,
        global_slots: asyncio.Semaphore,
        config: ToolExecutionConfig | None = None,
    ) -> None:
        self.config = config or ToolExecutionConfig()
        self.config.validate()
        self.registry = registry
        self.invoke_one = invoke_one
        self.bus = bus
        self.global_slots = global_slots
        self.run_slots = asyncio.Semaphore(self.config.max_concurrency_per_run)
        self.stats = ExecutionStats()

    # 验证输入后读取冻结策略，不可信工具和未知资源始终作为串行屏障
    def _claims(self, call: ToolCallBlock) -> tuple[ResourceClaim, ...] | None:
        policy = self.registry.concurrency_policy(call.name)
        if policy == "serialized" or (
            policy == "resource_scoped" and self.config.policy != "resource_scoped"
        ):
            return None
        tool = self.registry.get(call.name)
        if tool is None:
            return None
        try:
            self.registry.validate(call.name, call.input)
            claims = tool.resource_claims(call.input)
            if not claims or any(
                not isinstance(claim, ResourceClaim)
                or claim.mode not in {"read", "write"}
                or not claim.resource.is_absolute()
                for claim in claims
            ):
                return None
            if policy == "parallel_read" and any(claim.mode != "read" for claim in claims):
                return None
            root = getattr(tool, "_root", None)
            from pathlib import Path

            normalized = tuple(
                file_claim(root or Path.cwd(), str(c.resource), c.mode) for c in claims
            )
            if any(c is None for c in normalized) or normalized != claims:
                return None
            return claims
        except (ValueError, TypeError, OSError, RuntimeError):
            return None

    # 冲突边仅从先前调用指向后续调用，屏障阻止任意副作用跨越原始顺序
    def plan(self, calls: list[ToolCallBlock]) -> list[list[int]]:
        claims = [self._claims(call) for call in calls]
        dependencies: list[list[int]] = []
        for index, right in enumerate(claims):
            dependencies.append(
                [
                    earlier
                    for earlier, left in enumerate(claims[:index])
                    if left is None
                    or right is None
                    or any(a.conflicts(b) for a in left for b in right)
                ]
            )
        return dependencies

    # 将已限流执行的异常隔离为结果，取消继续向上传播且不重放工具
    async def _invoke(self, call: ToolCallBlock) -> ToolResult:
        token = current_result_budget.set(None)
        stats_token = current_execution_stats.set(self.stats)
        try:
            slots = () if call.name == "spawn_agent" else (self.run_slots, self.global_slots)
            return await self.invoke_one(call, slots)
        except asyncio.CancelledError:
            raise
        except Exception:
            journal = current_journal.get()
            if journal is not None:
                operation = journal.data["operations"].get(call.id)
                if operation is not None and operation["state"] == "executing":
                    journal.update(call.id, "unknown")
            return ToolResult(
                "Tool gateway failed; verify operation state before retry.",
                is_error=True,
                error_type="unknown_outcome",
            )
        finally:
            current_result_budget.reset(token)
            current_execution_stats.reset(stats_token)

    # 以有界任务组收敛批次，并由父任务按输入顺序分配唯一结果预算
    async def execute(
        self,
        calls: list[ToolCallBlock],
        *,
        run_id: str,
        step: int = 0,
        precomputed: dict[int, ToolResult] | None = None,
    ) -> list[ToolResult]:
        calls = deepcopy(calls)
        if not validate_call_ids(calls):
            get_collector().increment("tool_result_mismatch_total")
            return [
                ToolResult("Invalid or duplicate tool_use_id", True, "protocol_error")
                for _ in calls
            ]
        if len(calls) > self.config.max_calls_per_batch:
            return [ToolResult("Tool batch limit exceeded", True, "batch_limit") for _ in calls]
        if not calls:
            return []
        dependencies = (
            self.plan(calls)
            if self.config.parallel_enabled or self.config.shadow_enabled
            else [list(range(index)) for index in range(len(calls))]
        )
        batch_id = uuid.uuid4().hex
        theoretical = sum(not parents for parents in dependencies)
        await self.bus.publish(
            ToolBatchPlannedEvent(
                run_id=run_id,
                step=step,
                batch_id=batch_id,
                call_count=len(calls),
                theoretical_parallelism=min(theoretical, self.config.max_concurrency_per_run),
                mode="parallel"
                if self.config.parallel_enabled
                else ("shadow" if self.config.shadow_enabled else "serial"),
                ts=datetime.now(UTC).isoformat(),
            )
        )
        if not self.config.parallel_enabled:
            dependencies = [list(range(index)) for index in range(len(calls))]
        self.stats = ExecutionStats()
        collector = get_collector()
        collector.increment("tool_batch_size", len(calls))
        collector.increment("tool_conflict_serialized_total", sum(bool(p) for p in dependencies))
        started = time.monotonic()
        results: list[ToolResult | None] = [None] * len(calls)
        done = [asyncio.Event() for _ in calls]
        active, peak = 0, 0

        # 依赖满足后走唯一执行网关，预计算预算错误不调用工具
        async def run_one(index: int) -> None:
            nonlocal active, peak
            for parent in dependencies[index]:
                await done[parent].wait()
            active += 1
            peak = max(peak, active)
            try:
                result = (precomputed or {}).get(index)
                results[index] = result if result is not None else await self._invoke(calls[index])
            finally:
                active -= 1
                done[index].set()

        await self.bus.publish(
            ToolBatchStartedEvent(
                run_id=run_id,
                step=step,
                batch_id=batch_id,
                call_count=len(calls),
                ts=datetime.now(UTC).isoformat(),
            )
        )
        outcome = "completed"
        try:
            async with asyncio.TaskGroup() as group:
                for index in range(len(calls)):
                    group.create_task(run_one(index), name=f"tool:{calls[index].id}")
            ordered: list[ToolResult] = []
            budget = current_result_budget.get()
            journal = current_journal.get()
            for call, result in zip(calls, results, strict=True):
                assert result is not None
                if budget is not None:
                    result = budget.bound(result, call.id)
                    if journal is not None:
                        operation = journal.data["operations"].get(call.id)
                        if operation is not None and operation["state"] == "completed":
                            journal.update(
                                call.id,
                                "completed",
                                output=result.content,
                                is_error=result.is_error,
                                error_type=result.error_type,
                            )
                ordered.append(result)
            if any(result.is_error for result in ordered):
                outcome = "failed"
                collector.increment("tool_batch_failed_total")
            return ordered
        except asyncio.CancelledError:
            outcome = "cancelled"
            collector.increment("tool_batch_cancelled_total")
            raise
        except Exception:
            outcome = "failed"
            collector.increment("tool_batch_failed_total")
            raise
        finally:
            duration = (time.monotonic() - started) * 1000
            collector.record_timing("tool_batch_duration_ms", duration)
            collector.increment("tool_batch_parallelism", self.stats.peak)
            await self.bus.publish(
                ToolBatchFinishedEvent(
                    run_id=run_id,
                    step=step,
                    batch_id=batch_id,
                    call_count=len(calls),
                    outcome=outcome,
                    elapsed_ms=int(duration),
                    peak_in_flight=peak,
                    peak_executing=self.stats.peak,
                    ts=datetime.now(UTC).isoformat(),
                )
            )
