from __future__ import annotations

from claudedock.core.memory.model import MemoryEntry
from claudedock.core.memory.vector_store import MemoryVectorStore
from claudedock.core.rag.index import Bm25Index


# 构建零依赖 BM25 后端的向量层，供检索类测试复用
def _make_store() -> MemoryVectorStore:
    return MemoryVectorStore(Bm25Index(), chunk_size=200, chunk_overlap=20)


# 功能：验证 build 后 search 能按词法召回对应条目
# 设计：用 BM25 后端零网络依赖，检索 "英文" 应召回含该词的 preference 条目
async def test_build_and_search() -> None:
    store = _make_store()
    await store.build([
        MemoryEntry(id="e1", content="用户偏好使用英文作答", kind="preference"),
        MemoryEntry(id="e2", content="数据库迁移决定使用 PostgreSQL", kind="decision"),
    ])
    results = await store.search("英文", top_k=5)
    assert results
    assert results[0][0].id == "e1"


# 功能：验证 search 跳过非 active 状态的条目
# 设计：写入一条 expired 的英文条目，检索 "英文" 不应命中它
async def test_search_skips_non_active() -> None:
    store = _make_store()
    await store.build([
        MemoryEntry(id="e1", content="用户偏好使用英文作答", kind="preference", status="expired"),
        MemoryEntry(id="e2", content="数据库迁移决定使用 PostgreSQL", kind="decision"),
    ])
    results = await store.search("英文", top_k=5)
    assert all(e.id != "e1" for e, _ in results)


# 功能：验证 search 的 filter 参数限定 kind 候选集
# 设计：decision 与 knowledge 各含相关词，filter kind=knowledge 只召回 knowledge
async def test_search_filter_by_kind() -> None:
    store = _make_store()
    await store.build([
        MemoryEntry(id="e1", content="架构决策使用微服务", kind="decision"),
        MemoryEntry(id="e2", content="知识库说明微服务的部署方式", kind="knowledge"),
    ])
    results = await store.search("微服务", top_k=5, filter={"kind": "knowledge"})
    assert results
    assert all(e.kind == "knowledge" for e, _ in results)


# 功能：验证 filter 能按 tags 标签（列表元数据）过滤，命中含该标签的条目
# 设计：两条条目 tags 不同，filter={"tags": "ml"} 应只召回第一条；
#       列表型元数据此前被 canonical 成整串而恒不命中，此处锁定包含语义
async def test_search_filter_by_list_metadata() -> None:
    store = _make_store()
    await store.build([
        MemoryEntry(id="e1", content="模型训练使用蒸馏", kind="knowledge", tags=["ml", "nlp"]),
        MemoryEntry(id="e2", content="模型部署使用容器", kind="knowledge", tags=["ops"]),
    ])
    results = await store.search("模型", top_k=5, filter={"tags": "ml"})
    assert [e.id for e, _ in results] == ["e1"]


# 功能：验证 count 只统计 active 条目数
# 设计：一条 active 一条 expired，断言 count 为 1
async def test_count_active_only() -> None:
    store = _make_store()
    await store.build([
        MemoryEntry(id="e1", content="a", kind="fact"),
        MemoryEntry(id="e2", content="b", kind="fact", status="expired"),
    ])
    assert store.count() == 1


# 功能：验证空查询或空索引时 search 返回空列表而非抛错
# 设计：未 build 直接 search 空字符串，断言返回空列表，覆盖边界防御
async def test_search_empty_query() -> None:
    store = _make_store()
    assert await store.search("", top_k=5) == []


# 功能：验证 build 对内容未变的条目复用分块缓存，不重复 split_semantic
# 设计：monkeypatch split_semantic 计数，同内容二次 build 后调用次数不变
async def test_build_reuses_cached_chunks(monkeypatch) -> None:
    import claudedock.core.memory.vector_store as vs

    calls: list[str] = []
    orig = vs.split_semantic

    def counting(text: str, size: int, overlap: int) -> list[str]:
        calls.append(text)
        return orig(text, size, overlap)

    monkeypatch.setattr(vs, "split_semantic", counting)
    store = _make_store()
    entries = [MemoryEntry(id="e1", content="用户偏好使用英文作答", kind="preference")]
    await store.build(entries)
    n_after_first = len(calls)
    await store.build(entries)
    assert n_after_first == 1
    assert len(calls) == n_after_first
