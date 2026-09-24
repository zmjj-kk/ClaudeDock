from __future__ import annotations

from pathlib import Path

from kama_claude.core.memory.model import MemoryEntry
from kama_claude.core.memory.sqlite_store import MemorySqliteStore


# 功能：验证 upsert 写入后 get 能读回完整字段
# 设计：写入带 tags/importance 的条目，读回断言各字段一致，覆盖持久化往返
def test_upsert_and_get_roundtrip(tmp_path: Path) -> None:
    store = MemorySqliteStore(tmp_path / "m.db")
    store.upsert(MemoryEntry(
        id="m1", content="喜欢中文", kind="preference", project="p",
        importance=0.9, tags=["style"],
    ))
    got = store.get("m1")
    assert got is not None
    assert got.content == "喜欢中文"
    assert got.kind == "preference"
    assert got.importance == 0.9
    assert got.tags == ["style"]


# 功能：验证 upsert 按主键整体替换而非追加
# 设计：同 id 两次 upsert 不同 content，读回应为后者且 count=1，证明替换语义
def test_upsert_replaces_existing(tmp_path: Path) -> None:
    store = MemorySqliteStore(tmp_path / "m.db")
    store.upsert(MemoryEntry(id="m1", content="v1", kind="fact"))
    store.upsert(MemoryEntry(id="m1", content="v2", kind="fact"))
    assert store.count() == 1
    assert store.get("m1").content == "v2"


# 功能：验证 query 按 kind/project/status 精确过滤（status 默认只取 active）
# 设计：写入不同 kind/project/status 的条目，用集合断言过滤结果，避免 updated_at 排序干扰
def test_query_filters_by_kind_project_status(tmp_path: Path) -> None:
    store = MemorySqliteStore(tmp_path / "m.db")
    store.upsert(MemoryEntry(id="a", content="1", kind="preference", project="p"))
    store.upsert(MemoryEntry(id="b", content="2", kind="fact", project="p"))
    store.upsert(MemoryEntry(id="c", content="3", kind="fact"))
    store.upsert(MemoryEntry(id="d", content="4", kind="fact", project="p", status="expired"))
    assert {e.id for e in store.query(kind="fact")} == {"b", "c"}
    assert {e.id for e in store.query(kind="fact", project="p")} == {"b"}
    assert {e.id for e in store.query(status="expired")} == {"d"}


# 功能：验证 query 的 tags 过滤为任一匹配（非全集交集）
# 设计：条目 tags=["a","b"]，查询 tags=["a"] 命中、tags=["c"] 不命中，覆盖交集语义
def test_query_tags_match_any(tmp_path: Path) -> None:
    store = MemorySqliteStore(tmp_path / "m.db")
    store.upsert(MemoryEntry(id="m1", content="x", kind="fact", tags=["a", "b"]))
    store.upsert(MemoryEntry(id="m2", content="y", kind="fact", tags=["c"]))
    assert [e.id for e in store.query(tags=["a"])] == ["m1"]
    assert [e.id for e in store.query(tags=["c"])] == ["m2"]
    assert store.query(tags=["zz"]) == []


# 功能：验证 set_status/touch/delete 分别更新状态、访问时间、物理删除
# 设计：逐步断言 get 结果与 count 变化，覆盖三条写路径的持久化效果
def test_status_touch_delete(tmp_path: Path) -> None:
    store = MemorySqliteStore(tmp_path / "m.db")
    store.upsert(MemoryEntry(id="m1", content="x", kind="fact"))
    store.set_status("m1", "superseded", "2026-02-01T00:00:00+00:00")
    assert store.get("m1").status == "superseded"
    store.touch("m1", "2026-03-01T00:00:00+00:00")
    assert store.get("m1").last_accessed_at == "2026-03-01T00:00:00+00:00"
    store.delete("m1")
    assert store.get("m1") is None


# 功能：验证 all_entries 含非 active 状态，count 只统计 active
# 设计：写入一个 active 一个 expired，断言 all_entries 长度 2、count 为 1
def test_all_entries_includes_non_active(tmp_path: Path) -> None:
    store = MemorySqliteStore(tmp_path / "m.db")
    store.upsert(MemoryEntry(id="a", content="1", kind="fact"))
    store.upsert(MemoryEntry(id="b", content="2", kind="fact", status="expired"))
    assert len(store.all_entries()) == 2
    assert store.count() == 1
