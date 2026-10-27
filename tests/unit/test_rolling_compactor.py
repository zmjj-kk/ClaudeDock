from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from claudedock.core.compact.compactor import Compactor
from claudedock.core.context import ExecutionBudget, ExecutionContext, current_budget
from claudedock.core.events.bus import EventBus
from claudedock.core.llm.governed import BudgetExceeded
from claudedock.core.llm.types import LlmResponse, UsageStats

SUMMARY = "\n".join(
    f"## {index}. {title}\nNone"
    for index, title in enumerate((
        "Original Goal", "Completed Steps", "Key Constraints & Discoveries",
        "Current File State", "Remaining TODOs", "Critical Data",
    ), 1)
)


# 构造足够旧证据，使保留用户原文后仍有可测压缩收益
def make_history(turns: int = 10) -> list[dict[str, Any]]:
    return [
        message for turn in range(turns) for message in (
            {"role": "user", "content": f"constraint {turn}"},
            {"role": "assistant", "content": "evidence " * 1000},
        )
    ]


# 功能：验证滚动摘要保留最近原文与全部用户约束，摘要不覆盖不可变目标
# 设计：摘要故意不提约束，断言确定性锚点仍逐项保留
async def test_rolling_anchor_and_recent_messages(tmp_path: Path) -> None:
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse("end_turn", text=SUMMARY))
    context = ExecutionContext("run", "immutable goal", 20, messages=make_history())
    original = deepcopy(context.messages)
    compactor = Compactor(EventBus(), tmp_path, "s", recent_turns=4, enforce_gain=True)
    result = await compactor.compact(context, provider)
    assert result is not None
    assert context.messages[-8:] == original[-8:]
    assert context.goal == "immutable goal"
    for turn in range(6):
        assert f"constraint {turn}" in context.messages[0]["content"]


# 功能：验证提交失败不会提前修改上下文或发布成功事件
# 设计：注入磁盘写失败回调，保留原文和版本号以覆盖事务回滚
async def test_checkpoint_failure_retains_original(tmp_path: Path) -> None:
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse("end_turn", text=SUMMARY))
    context = ExecutionContext("run", "goal", 20, messages=make_history())
    original = deepcopy(context.messages)

    # 模拟投影提交阶段磁盘写入失败
    def fail(messages: list[dict[str, Any]]) -> None:
        raise OSError("disk full")

    compactor = Compactor(EventBus(), tmp_path, "s", checkpoint=fail, recent_turns=4)
    assert await compactor.compact(context, provider) is None
    assert context.messages == original
    assert context.compaction_version == 0


# 功能：验证连续无效摘要熔断且费用令牌仍计入累计预算
# 设计：持续返回空摘要，三次之后不得继续请求提供者
async def test_failure_circuit_and_failed_summary_usage(tmp_path: Path) -> None:
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(
        "end_turn", text="", usage=UsageStats(100, 10),
    ))
    context = ExecutionContext("run", "goal", 20, messages=make_history())
    budget = ExecutionBudget()
    token = current_budget.set(budget)
    try:
        compactor = Compactor(EventBus(), tmp_path, "s", max_failures=3)
        for _ in range(5):
            assert await compactor.compact(context, provider, hard_limit=True) is None
        assert provider.chat.await_count == 3
        assert budget.tokens == 330
        assert budget.model_calls == 3
    finally:
        current_budget.reset(token)


# 功能：验证已耗尽累计预算时不能通过压缩额外调用模型
# 设计：直接设置共享执行预算耗尽，保证发送请求之前拒绝
async def test_summary_cannot_bypass_execution_budget(tmp_path: Path) -> None:
    provider = MagicMock()
    provider.chat = AsyncMock()
    token = current_budget.set(ExecutionBudget(max_model_calls=0))
    try:
        with pytest.raises(BudgetExceeded):
            await Compactor(EventBus(), tmp_path, "s").compact_messages(make_history(), provider)
        provider.chat.assert_not_awaited()
    finally:
        current_budget.reset(token)

# 功能：验证六段摘要不能在没有工具证据时伪造测试已通过
# 设计：仅替换已完成步骤，确保格式校验通过后仍被证据校验拒绝
async def test_fabricated_test_success_rejected(tmp_path: Path) -> None:
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(
        "end_turn", text=SUMMARY.replace("## 2. Completed Steps\nNone", "## 2. Completed Steps\nAll tests passed"),
    ))
    compactor = Compactor(EventBus(), tmp_path, "s", recent_turns=4, enforce_gain=True)
    assert await compactor.compact_messages(make_history(), provider) is None
