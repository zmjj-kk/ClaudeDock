from __future__ import annotations

import hashlib

from kama_claude.core.memory.model import MemoryEntry
from kama_claude.core.rag.chunk import split_semantic
from kama_claude.core.rag.filter import MetaValue, matches
from kama_claude.core.rag.index import RagIndex

# 语义检索时额外多取回的候选倍数，补偿按条目去重带来的召回损失
_DEDUP_DEPTH_FACTOR = 4


class MemoryVectorStore:
    # 绑定底层 RagIndex 与分块参数；长文本经 split_semantic 切片后交给 index 建索引
    def __init__(self, index: RagIndex, *, chunk_size: int, chunk_overlap: int) -> None:
        self._index = index
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._texts: list[str] = []
        self._entry_ids: list[str] = []
        self._metas: list[dict[str, MetaValue]] = []
        self._entries: dict[str, MemoryEntry] = {}

    # 全量重建索引：逐条目切片并记录 chunk→条目映射与可过滤元数据
    async def build(self, entries: list[MemoryEntry]) -> None:
        texts: list[str] = []
        entry_ids: list[str] = []
        metas: list[dict[str, MetaValue]] = []
        by_id: dict[str, MemoryEntry] = {}
        for e in entries:
            by_id[e.id] = e
            chunks = split_semantic(e.content, self._chunk_size, self._chunk_overlap)
            if not chunks:
                chunks = [e.content.strip()] if e.content.strip() else []
            for text in chunks:
                texts.append(text)
                entry_ids.append(e.id)
                metas.append(_meta_for(e))
        self._texts = texts
        self._entry_ids = entry_ids
        self._metas = metas
        self._entries = by_id
        ids = [f"{entry_ids[i]}::{i}" for i in range(len(texts))]
        fingerprint = _fingerprint(entries)
        await self._index.build(texts, ids, fingerprint, metas)

    # 语义检索：filter 先翻译为 chunk 白名单，交 index 召回后按条目去重取最高分
    async def search(
        self,
        query: str,
        top_k: int,
        filter: dict[str, object] | None = None,
    ) -> list[tuple[MemoryEntry, float]]:
        if not self._texts or not query.strip():
            return []
        cond = dict(filter) if filter else {}
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
        out = set(range(len(self._texts)))
        for key, val in cond.items():
            hit = {
                i for i, m in enumerate(self._metas) if key in m and matches(m[key], val)
            }
            out &= hit
        return out


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
