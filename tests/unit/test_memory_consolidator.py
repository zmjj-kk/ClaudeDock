from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from claudedock.core.config import MemoryConfig
from claudedock.core.llm.types import LlmResponse, UsageStats
from claudedock.core.memory.consolidator import _MAX_REVIEW, Consolidator, _parse_plan
from claudedock.core.memory.model import MemoryEntry


def _entries() -> list[MemoryEntry]:
    return [
        MemoryEntry(id="a", content="用户偏好英文", kind="preference", importance=0.6),
        MemoryEntry(id="b", content="用户希望英文作答", kind="preference", importance=0.8),
    ]


# 功能：验证 _parse_plan 从带噪声的文本中剥离出 JSON 对象
# 设计：LLM 输出常带解释前缀，用 find/rfind 定位花括号，断言解析结果字段正确
def test_parse_plan_strips_noise() -> None:
    text = '好的：\n{"merge": [], "retire": [{"id": "a", "status": "expired"}]}\n以上。'
    plan = _parse_plan(text)
    assert plan is not None
    assert plan["merge"] == []
    assert plan["retire"] == [{"id": "a", "status": "expired"}]


# 功能：验证 _parse_plan 对无 JSON 或非法 JSON 返回 None
# 设计：传入纯文本与坏 JSON 各一，断言均为 None，避免整合器崩溃
def test_parse_plan_invalid_returns_none() -> None:
    assert _parse_plan("no json here") is None
    assert _parse_plan("") is None
    assert _parse_plan("{not valid json}") is None


# 功能：验证 _apply_plan 把多条合并为一条，并退休/删除旧条目
# 设计：merge 计划引用两条 id，断言 replace 一条、retire 两条、delete 两条
def test_apply_plan_merge() -> None:
    c = Consolidator(MemoryConfig())
    plan = {
        "merge": [{
            "ids": ["a", "b"],
            "content": "用户偏好英文作答",
            "kind": "preference",
            "importance": 0.8,
            "tags": ["style"],
        }],
        "retire": [],
    }
    result = c._apply_plan(plan, _entries())
    assert len(result.replace) == 1
    assert result.replace[0].content == "用户偏好英文作答"
    assert sorted(result.retire) == [("a", "superseded"), ("b", "superseded")]
    assert sorted(result.delete) == ["a", "b"]


# 功能：验证 _apply_plan 对 retire 计划只标记状态不删除
# 设计：retire 计划引用一条 id，断言 retire 一条、delete 为空
def test_apply_plan_retire() -> None:
    c = Consolidator(MemoryConfig())
    result = c._apply_plan(
        {"merge": [], "retire": [{"id": "a", "status": "expired"}]}, _entries()
    )
    assert result.replace == []
    assert result.retire == [("a", "expired")]
    assert result.delete == []


# 功能：验证 _apply_plan 忽略引用不存在 id 的计划项
# 设计：retire 计划引用未知 id，断言结果为空，防止越界引用污染存储
def test_apply_plan_ignores_unknown_id() -> None:
    c = Consolidator(MemoryConfig())
    result = c._apply_plan(
        {"merge": [], "retire": [{"id": "zzz", "status": "expired"}]}, _entries()
    )
    assert result.retire == []


# 功能：验证 consolidate 调用 stub provider 返回整合结果
# 设计：provider 返回固定 JSON，断言结果 retire 计数正确
async def test_consolidate_with_stub_provider() -> None:
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(
        stop_reason="end_turn",
        text='{"merge": [], "retire": [{"id": "a", "status": "expired"}]}',
        usage=UsageStats(input_tokens=10, output_tokens=10),
    ))
    c = Consolidator(MemoryConfig())
    result = await c.consolidate(provider, _entries())
    assert result is not None
    assert result.retire == [("a", "expired")]


# 功能：验证 provider 抛异常时 consolidate 返回 None 不阻断
# 设计：provider.chat 抛 RuntimeError，断言返回 None
async def test_consolidate_provider_failure_returns_none() -> None:
    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=RuntimeError("boom"))
    c = Consolidator(MemoryConfig())
    result = await c.consolidate(provider, _entries())
    assert result is None


# 功能：验证 consolidate 分批审查全部 active 条目，超过 _MAX_REVIEW 的条目也能被退休
# 设计：构造 _MAX_REVIEW+1 条记忆，retire 目标放在最后一批，断言 retire 生效且 provider 被调用两次
async def test_consolidate_reviews_entries_beyond_first_batch() -> None:
    entries = [
        MemoryEntry(id=f"e{i:04d}", content=f"记忆 {i}", kind="fact")
        for i in range(_MAX_REVIEW + 1)
    ]
    target = entries[-1].id
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(
        stop_reason="end_turn",
        text=json.dumps({"merge": [], "retire": [{"id": target, "status": "superseded"}]}),
        usage=UsageStats(input_tokens=10, output_tokens=10),
    ))
    c = Consolidator(MemoryConfig())
    result = await c.consolidate(provider, entries)
    assert result is not None
    assert result.retire == [(target, "superseded")]
    assert provider.chat.await_count == 2
