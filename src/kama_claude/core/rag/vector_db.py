from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from kama_claude.core.rag.index import RagIndex, ZhipuEmbeddingIndex, hash_text

# Qdrant 点的默认集合名
_COLLECTION = "kama_chunks"
# 稳定 id 派生 point id 的命名空间：Qdrant 只接受 UUID/uint，用 uuid5 把 "doc_id::i" 压成 UUID
_NS = uuid.NAMESPACE_URL

log = logging.getLogger(__name__)


# 将稳定 id 确定性映射为 Qdrant 可接受的 UUID point id
def _point_id(sid: str) -> str:
    return str(uuid.uuid5(_NS, sid))


class QdrantIndex(RagIndex):
    # 初始化 Qdrant 客户端：embed 复用智谱 embedding 生成，Qdrant 只接管存储与检索
    def __init__(
        self,
        embed: ZhipuEmbeddingIndex,
        *,
        url: str = "",
        path: str = "~/.kama/qdrant",
        collection: str = _COLLECTION,
    ) -> None:
        try:
            from qdrant_client import QdrantClient, models
        except ImportError as exc:  # pragma: no cover - 依赖缺失时给出可操作提示
            raise ImportError(
                "缺少 qdrant-client：运行 `uv add qdrant-client` 或 `pip install qdrant-client`"
            ) from exc
        self._embed = embed
        self._collection = collection
        self._models = models
        self._client = (
            QdrantClient(url=url) if url else QdrantClient(path=str(Path(path).expanduser()))
        )
        self._idx_map: dict[str, int] = {}

    # 确保集合存在：按 embedding 维度建 cosine 距离的集合，重复调用幂等
    def _ensure_collection(self) -> None:
        if self._client.collection_exists(self._collection):
            return
        self._client.create_collection(
            collection_name=self._collection,
            vectors_config=self._models.VectorParams(
                size=self._embed._dimensions, distance=self._models.Distance.COSINE
            ),
        )

    # 拉取库中全部点的 {point_id: content_hash} 映射，用于增量比对与清理
    def _existing_hashes(self) -> dict[str, str]:
        existing: dict[str, str] = {}
        offset: Any = None
        while True:
            points, offset = self._client.scroll(
                collection_name=self._collection,
                limit=512,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for p in points:
                payload = p.payload or {}
                existing[str(p.id)] = str(payload.get("content_hash", ""))
            if offset is None:
                break
        return existing

    # 构建索引：按稳定 id + content_hash 增量 upsert，仅新增/变更 chunk 调 embedding
    async def build(self, texts: list[str], ids: list[str], fingerprint: str) -> None:
        del fingerprint
        await asyncio.to_thread(self._ensure_collection)
        existing = await asyncio.to_thread(self._existing_hashes)
        point_ids = [_point_id(sid) for sid in ids]
        hashes = [hash_text(t) for t in texts]
        to_embed = [i for i in range(len(texts)) if existing.get(point_ids[i]) != hashes[i]]
        if to_embed:
            vecs = await self._embed._embed([texts[i] for i in to_embed])
            points = [
                self._models.PointStruct(
                    id=point_ids[i],
                    vector=vecs[j],
                    payload={"stable_id": ids[i], "content_hash": hashes[i]},
                )
                for j, i in enumerate(to_embed)
            ]
            await asyncio.to_thread(self._client.upsert, self._collection, points)
        stale = set(existing) - set(point_ids)
        if stale:
            await asyncio.to_thread(self._client.delete, self._collection, list(stale))
        self._idx_map = {point_ids[i]: i for i in range(len(ids))}

    # 检索与 query 最相似的 top_k 个点，把 point id 映射回 texts 下标返回；
    # allowed 限定候选下标白名单时，扩大召回上限再过滤，保证过滤后仍能取满 top_k
    async def search(
        self, query: str, top_k: int, allowed: set[int] | None = None
    ) -> list[tuple[int, float]]:
        if not self._idx_map or not query.strip():
            return []
        if allowed is not None and not allowed:
            return []
        qvec = (await self._embed._embed([query]))[0]
        limit = top_k if allowed is None else len(self._idx_map)
        resp = await asyncio.to_thread(
            self._client.query_points,
            collection_name=self._collection,
            query=qvec,
            limit=limit,
        )
        out: list[tuple[int, float]] = []
        for hit in resp.points:
            key = str(hit.id)
            if key in self._idx_map:
                idx = self._idx_map[key]
                if allowed is not None and idx not in allowed:
                    continue
                out.append((idx, float(hit.score)))
                if len(out) >= top_k:
                    break
        return out
