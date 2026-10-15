from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from kama_claude.core.compact.token_budget import estimate_tokens
from kama_claude.core.config import MemoryConfig
from kama_claude.core.memory.store import MemoryStore


# 功能：验证过期、跨项目记忆被过滤且注入不超过预算
# 设计：混合偏好、过期事实与另一项目条目，对真实 SQLite 门面执行查询
async def test_expiry_scope_and_injection_budget(tmp_path: Path) -> None:
    store = MemoryStore(MemoryConfig(store_dir=str(tmp_path)))
    try:
        await store.add("current preference", kind="preference", project="p")
        await store.add("other project secret", kind="preference", project="other")
        await store.add(
            "expired fact", kind="fact", project="p",
            expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
        )
        text = await store.inject(project="p", token_budget=256)
        assert "current preference" in text
        assert "other project" not in text and "expired" not in text
        assert estimate_tokens(text) <= 256
        assert not await store.query(kind="fact", project="p")
    finally:
        await store.aclose()


# 功能：验证两个项目的同名同内容记忆不会因内容哈希合并串线
# 设计：对同一内容连续写入不同项目，比较权威记录标识与查询范围
async def test_same_content_has_project_identity(tmp_path: Path) -> None:
    store = MemoryStore(MemoryConfig(store_dir=str(tmp_path)))
    try:
        first = await store.add("shared name", kind="preference", project="a")
        second = await store.add("shared name", kind="preference", project="b")
        assert first is not None and second is not None and first.id != second.id
        assert [item.project for item in await store.query(project="a")] == ["a"]
    finally:
        await store.aclose()

# 功能：验证记忆撤销立即影响检索且不能撤销另一个项目的条目
# 设计：先做负向授权检查，再撤销当前项目条目并重查真实存储
async def test_retire_respects_project_scope(tmp_path: Path) -> None:
    import pytest

    store = MemoryStore(MemoryConfig(store_dir=str(tmp_path)))
    try:
        entry = await store.add("a fact", kind="fact", project="a")
        assert entry is not None
        with pytest.raises(ValueError):
            await store.retire(entry.id, "b")
        await store.retire(entry.id, "a")
        assert not await store.query(project="a")
    finally:
        await store.aclose()
