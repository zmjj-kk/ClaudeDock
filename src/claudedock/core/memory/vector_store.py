from __future__ import annotations

import asyncio
import copy
import hashlib
import logging

from claudedock.core.memory.model import MemoryEntry
from claudedock.core.rag.chunk import split_semantic
from claudedock.core.rag.filter import MetaValue, select
from claudedock.core.rag.index import RagIndex

# 语义检索时额外多取回的候选倍数，补偿按条目去重带来的召回损失
_DEDUP_DEPTH_FACTOR = 4


class MemoryVectorStore:
    # 绑定底层 RagIndex 与分块参数；长文本经 split_semantic 切片后交给 index 建索引
    def __init__(self, index: RagIndex, *, chunk_size: int, chunk_overlap: int) -> None:
        self._refresh_lock = asyncio.Lock()
        self._readers: dict[int, int] = {}
        self._retired: list[RagIndex] = []
        self._index = index
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._texts: list[str] = []
        self._entry_ids: list[str] = []
        self._metas: list[dict[str, MetaValue]] = []
        self._entries: dict[str, MemoryEntry] = {}
        # 条目 id → (content, chunks) 缓存，内容不变时跳过重复分块，降低增量写入成本
        self._chunks_cache: dict[str, tuple[str, list[str]]] = {}

    # 关闭底层向量索引，释放其网络/存储资源
    async def aclose(self) -> None:
        for index in self._retired:
            await self._retire(index)
        self._retired.clear()
        await self._index.aclose()

    # 全量重建索引：逐条目切片并记录 chunk→条目映射与可过滤元数据
    async def build(self, entries: list[MemoryEntry]) -> None:
        async with self._refresh_lock:
            await self._build(entries)

    # 完整派生版本准备成功后发布并回收不再被读取的旧索引
    async def _build(self, entries: list[MemoryEntry]) -> None:
        texts: list[str] = []
        entry_ids: list[str] = []
        metas: list[dict[str, MetaValue]] = []
        by_id: dict[str, MemoryEntry] = {}
        next_cache: dict[str, tuple[str, list[str]]] = {}
        for e in entries:
            by_id[e.id] = e
            # 内容未变的条目复用上次分块结果，避免逐次全量重复 split_semantic
            cached = self._chunks_cache.get(e.id)
            if cached is not None and cached[0] == e.content:
                chunks = cached[1]
                next_cache[e.id] = cached
            else:
                chunks = split_semantic(e.content, self._chunk_size, self._chunk_overlap)
                if not chunks:
                    chunks = [e.content.strip()] if e.content.strip() else []
                next_cache[e.id] = (e.content, chunks)
            for text in chunks:
                texts.append(text)
                entry_ids.append(e.id)
                metas.append(_meta_for(e))
        ids = [f"{entry_ids[i]}::{i}" for i in range(len(texts))]
        index = self._index.fork()
        try:
            await index.build(texts, ids, _fingerprint(entries), metas)
        except BaseException:
            await index.retire()
            raise
        # 读取路径采用完整对象快照，元数据与底层索引不会交错
        previous = self._index
        self._texts = texts
        self._entry_ids = entry_ids
        self._metas = metas
        self._entries = by_id
        self._chunks_cache = next_cache
        self._index = index
        if self._readers.get(id(previous), 0):
            self._retired.append(previous)
        else:
            await self._retire(previous)

    # 派生数据清理失败只记录状态，不回滚已提交的权威记忆
    async def _retire(self, index: RagIndex) -> None:
        try:
            await index.retire()
        except Exception:
            logging.getLogger(__name__).warning("Memory snapshot cleanup failed", exc_info=True)

    # 语义检索：filter 先翻译为 chunk 白名单，交 index 召回后按条目去重取最高分
    async def search(
        self,
        query: str,
        top_k: int,
        filter: dict[str, object] | None = None,
    ) -> list[tuple[MemoryEntry, float]]:
        snapshot = copy.copy(self)
        index = snapshot._index
        key = id(index)
        self._readers[key] = self._readers.get(key, 0) + 1
        try:
            return await snapshot._search_snapshot(query, top_k, filter)
        finally:
            self._readers[key] -= 1
            if self._readers[key] == 0:
                self._readers.pop(key)
                if index in self._retired:
                    self._retired.remove(index)
                    await self._retire(index)

    # 对固定版本的条目映射与索引执行查询
    async def _search_snapshot(
        self,
        query: str,
        top_k: int,
        filter: dict[str, object] | None,
    ) -> list[tuple[MemoryEntry, float]]:
        if not self._texts or not query.strip():
            return []
        cond = dict(filter) if isinstance(filter, dict) else {}
        allowed = self._match(cond) if cond else None
        if allowed is not None and not allowed:
            return []
        depth = max(top_k, top_k * _DEDUP_DEPTH_FACTOR)
        scored = await self._index.search(query, depth, allowed, cond or None)
        best: dict[str, tuple[MemoryEntry, float]] = {}
        order: list[str] = []
        for idx, score in scored:
            eid = self._entry_ids[idx]
            entry = self._entries.get(eid)
            if entry is None or entry.status != "active":
                continue
            if eid not in best:
                best[eid] = (entry, score)
                order.append(eid)
            elif score > best[eid][1]:
                best[eid] = (entry, score)
        return [best[eid] for eid in order[:top_k]]

    # 返回当前索引中的 active 条目数
    def count(self) -> int:
        return sum(1 for e in self._entries.values() if e.status == "active")

    # 把过滤条件翻译为命中 chunk 下标集合，供内存后端二次筛选（线性扫描，量级千级足够）
    def _match(self, cond: dict[str, object]) -> set[int]:
        return select(self._metas, cond)


# 从条目组装 chunk 可过滤元数据，供向量库服务端过滤下推
def _meta_for(entry: MemoryEntry) -> dict[str, MetaValue]:
    return {
        "kind": entry.kind,
        "project": entry.project,
        "tags": list(entry.tags),
        "importance": entry.importance,
    }


# 由全部条目内容派生构建指纹：内容或更新时间变化时指纹改变，触发索引重建
def _fingerprint(entries: list[MemoryEntry]) -> str:
    h = hashlib.sha256()
    for e in sorted(entries, key=lambda x: x.id):
        h.update(e.id.encode("utf-8"))
        h.update(e.content.encode("utf-8"))
        h.update(e.updated_at.encode("utf-8"))
    return h.hexdigest()
