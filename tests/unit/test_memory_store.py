from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from claudedock.core.config import MemoryConfig
from claudedock.core.llm.types import LlmResponse, UsageStats
from claudedock.core.memory.sqlite_store import MemorySqliteStore
from claudedock.core.memory.store import MemoryStore
from claudedock.core.memory.vector_store import MemoryVectorStore
from claudedock.core.rag.index import Bm25Index


# 组装带双层存储的 MemoryStore，sqlite 与向量各自可独立引用，供断言内部状态
def _make_store(tmp_path: Path, *, max_entries: int = 100) -> tuple[MemoryStore, MemorySqliteStore]:
    config = MemoryConfig(store_dir=str(tmp_path), max_entries=max_entries)
    sqlite = MemorySqliteStore(tmp_path / "memories.db")
    vector = MemoryVectorStore(Bm25Index(), chunk_size=200, chunk_overlap=20)
    store = MemoryStore(config, sqlite_store=sqlite, vector_store=vector)
    return store, sqlite


# 功能：验证 add 按 kind 路由：preference 只入 sqlite，knowledge 入向量可语义检索
# 设计：写入 preference 与 knowledge 各一条，search 只召回 knowledge，query 能查到 preference
async def test_add_routes_by_kind(tmp_path: Path) -> None:
    store, _ = _make_store(tmp_path)
    await store.load()
    await store.add("用户偏好使用英文", kind="preference")
    await store.add("产品文档位于 docs 目录", kind="knowledge")
    hits = await store.search("文档", top_k=5)
    assert hits
    assert all(e.kind == "knowledge" for e in hits)
    prefs = await store.query(kind="preference")
    assert [e.content for e in prefs] == ["用户偏好使用英文"]


# 功能：验证 add 相同内容二次写入返回 None（内容 hash 去重）
# 设计：同 content 两次 add，第二次返回 None 且 count 仍为 1
async def test_add_dedup_by_content(tmp_path: Path) -> None:
    store, _ = _make_store(tmp_path)
    await store.load()
    first = await store.add("用户偏好使用英文", kind="preference")
    second = await store.add("用户偏好使用英文", kind="preference")
    assert first is not None
    assert second is None
    assert store.count() == 1


# 功能：验证 add 对空内容返回 None 且不落库
# 设计：content 仅空白，断言返回 None 且 count 为 0
async def test_add_rejects_blank_content(tmp_path: Path) -> None:
    store, _ = _make_store(tmp_path)
    await store.load()
    assert await store.add("   ", kind="fact") is None
    assert store.count() == 0


# 功能：验证 inject 拼接高重要度偏好为纯文本块
# 设计：写入 preference，inject 返回的字符串包含其正文，供 System Prompt 注入
async def test_inject_returns_preferences(tmp_path: Path) -> None:
    store, _ = _make_store(tmp_path)
    await store.load()
    await store.add("用户偏好使用英文作答", kind="preference", importance=0.9)
    block = await store.inject(top_n=5)
    assert "用户偏好使用英文作答" in block


# 功能：验证 count 达到 max_entries 阈值时 needs_consolidation 为 True
# 设计：max_entries=1，写入一条后断言 needs_consolidation 为 True
async def test_needs_consolidation_threshold(tmp_path: Path) -> None:
    store, _ = _make_store(tmp_path, max_entries=1)
    await store.load()
    await store.add("一条记忆", kind="fact")
    assert store.count() == 1
    assert store.needs_consolidation() is True


# 功能：验证 consolidate 用 stub provider 的 retire 计划标记条目状态
# 设计：provider 返回 retire 一条的 JSON，consolidate 后该条目 status 变为 superseded 且 count 归零
async def test_consolidate_applies_retire(tmp_path: Path) -> None:
    store, sqlite = _make_store(tmp_path)
    await store.load()
    entry = await store.add("旧结论：使用 MySQL", kind="decision")
    assert entry is not None
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(
        stop_reason="end_turn",
        text=json.dumps({"merge": [], "retire": [{"id": entry.id, "status": "superseded"}]}),
        usage=UsageStats(input_tokens=10, output_tokens=10),
    ))
    result = await store.consolidate(provider)
    assert result is not None
    assert result.retire == [(entry.id, "superseded")]
    assert store.count() == 0
    assert sqlite.get(entry.id).status == "superseded"


# 功能：验证 provider 返回空计划时 consolidate 返回非空结果且不改变存储
# 设计：空 merge/retire JSON，断言结果 replace/retire 均为空、count 不变
async def test_consolidate_empty_plan_is_noop(tmp_path: Path) -> None:
    store, _ = _make_store(tmp_path)
    await store.load()
    await store.add("一条记忆", kind="fact")
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(
        stop_reason="end_turn",
        text='{"merge": [], "retire": []}',
        usage=UsageStats(input_tokens=10, output_tokens=10),
    ))
    result = await store.consolidate(provider)
    assert result is not None
    assert result.replace == []
    assert result.retire == []
    assert store.count() == 1


# 功能：验证 inject 按重要度而非更新时间挑选偏好
# 设计：写入两条 importance 差异明显的偏好，top_n=1 时只应注入高重要度那条
async def test_inject_ranks_preferences_by_importance(tmp_path: Path) -> None:
    store, _ = _make_store(tmp_path)
    await store.load()
    await store.add("高重要度偏好", kind="preference", importance=0.9)
    await store.add("低重要度偏好", kind="preference", importance=0.2)
    block = await store.inject(top_n=1)
    assert "高重要度偏好" in block
    assert "低重要度偏好" not in block


# 功能：验证 load 只把 active 条目纳入向量索引，退休条目不再嵌入
# 设计：写入一条 knowledge 后置为 superseded，load 后 _vector_entries 应为空
async def test_load_excludes_retired_from_vector(tmp_path: Path) -> None:
    store, sqlite = _make_store(tmp_path)
    await store.load()
    entry = await store.add("一条知识", kind="knowledge")
    assert entry is not None
    sqlite.set_status(entry.id, "superseded", "2026-01-01T00:00:00+00:00")
    await store.load()
    assert store._vector_entries == []


# 功能：验证 inject 按项目维度过滤：注入当前项目 + 全局记忆，排除其它项目
# 设计：写入项目 A、项目 B、全局三条偏好，inject(project="A") 只含 A 与全局
async def test_inject_filters_by_project(tmp_path: Path) -> None:
    store, _ = _make_store(tmp_path)
    await store.load()
    await store.add("项目 A 偏好", kind="preference", project="A", importance=0.9)
    await store.add("项目 B 偏好", kind="preference", project="B", importance=0.9)
    await store.add("全局偏好", kind="preference", importance=0.9)
    block = await store.inject(top_n=10, project="A")
    assert "项目 A 偏好" in block
    assert "全局偏好" in block
    assert "项目 B 偏好" not in block
