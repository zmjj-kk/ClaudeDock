from __future__ import annotations

import asyncio
import copy
import logging
import os
import uuid
from pathlib import Path
from typing import Any, cast

from claudedock.core.rag.filter import is_numeric_range, is_range, select
from claudedock.core.rag.index import RagIndex, ZhipuEmbeddingIndex, hash_text

# Qdrant 点的默认集合名
_COLLECTION = "claudedock_chunks"
# 稳定 id 派生 point id 的命名空间：Qdrant 只接受 UUID/uint，用 uuid5 把 "doc_id::i" 压成 UUID
_NS = uuid.NAMESPACE_URL

log = logging.getLogger(__name__)


# 将稳定 id 确定性映射为 Qdrant 可接受的 UUID point id
def _point_id(sid: str) -> str:
    return str(uuid.uuid5(_NS, sid))


# 组装 Qdrant 点 payload：稳定 id、内容 hash、可过滤元数据（值已类型化）
def _payload(sid: str, content_hash: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {"stable_id": sid, "content_hash": content_hash, "meta": meta}


class QdrantIndex(RagIndex):
    # 初始化 Qdrant 客户端：embed 复用智谱 embedding 生成，Qdrant 只接管存储与检索
    def __init__(
        self,
        embed: ZhipuEmbeddingIndex,
        *,
        url: str = "",
        path: str = "~/.claudedock/qdrant",
        collection: str = _COLLECTION,
    ) -> None:
        try:
            from qdrant_client import QdrantClient, models
        except ImportError as exc:  # pragma: no cover - 依赖缺失时给出可操作提示
            raise ImportError(
                "缺少 qdrant-client：运行 `uv add qdrant-client` 或 `pip install qdrant-client`"
            ) from exc
        self._owned_collections: set[str] = set()
        self._embed = embed
        self._collection = collection
        self._models = models
        self._client = (
            QdrantClient(url=url, api_key=os.environ.get("QDRANT_API_KEY"))
            if url
            else QdrantClient(path=str(Path(path).expanduser()))
        )
        self._idx_map: dict[str, int] = {}
        # 本地保留各点元数据：Range 无法表达的过滤条件在此回落为白名单二次筛选
        self._metas: list[dict[str, Any]] = []

    # 在独立集合中构建新版本，失败不会修改旧集合
    def fork(self) -> RagIndex:
        if len(self._owned_collections) >= 128:
            raise RuntimeError("Qdrant snapshot retention limit reached; restart to reclaim")
        snapshot = copy.copy(self)
        prefix = self._collection.split("__snapshot_")[0]
        snapshot._collection = f"{prefix}__snapshot_{uuid.uuid4().hex}"
        snapshot._idx_map = {}
        snapshot._metas = []
        self._owned_collections.add(snapshot._collection)
        return snapshot

    # 最后一个查询释放旧版本后删除该快照自己的集合
    async def retire(self) -> None:
        if self._collection not in self._owned_collections:
            return
        if await asyncio.to_thread(self._client.collection_exists, self._collection):
            await asyncio.to_thread(self._client.delete_collection, self._collection)
        self._owned_collections.discard(self._collection)

    # 关闭 Qdrant 客户端与复用的 embedding 客户端
    async def aclose(self) -> None:
        for collection in list(self._owned_collections):
            if self._client.collection_exists(collection):
                await asyncio.to_thread(self._client.delete_collection, collection)
        self._client.close()
        await self._embed.aclose()

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

    # 拉取库中全部点的 {point_id: payload} 映射，用于增量比对、meta 变更检测与清理
    def _existing_payloads(self) -> dict[str, dict[str, Any]]:
        existing: dict[str, dict[str, Any]] = {}
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
                existing[str(p.id)] = p.payload or {}
            if offset is None:
                break
        return existing

    # 构建索引：按稳定 id + content_hash 增量 upsert，仅新增/变更 chunk 调 embedding；
    # 元数据写入 payload 供检索时服务端过滤下推；meta 变更仅更新 payload 不重算向量
    async def build(
        self,
        texts: list[str],
        ids: list[str],
        fingerprint: str,
        metas: list[dict[str, Any]] | None = None,
    ) -> None:
        del fingerprint
        await asyncio.to_thread(self._ensure_collection)
        existing = await asyncio.to_thread(self._existing_payloads)
        point_ids = [_point_id(sid) for sid in ids]
        hashes = [hash_text(t) for t in texts]
        metas = metas if metas is not None else [{} for _ in texts]
        # 内容变化（含新增）→ 重新 embedding + upsert，写入完整 payload
        to_embed = [
            i
            for i in range(len(texts))
            if existing.get(point_ids[i], {}).get("content_hash") != hashes[i]
        ]
        if to_embed:
            vecs = await self._embed._embed([texts[i] for i in to_embed])
            points = [
                self._models.PointStruct(
                    id=point_ids[i],
                    vector=vecs[j],
                    payload=_payload(ids[i], hashes[i], metas[i]),
                )
                for j, i in enumerate(to_embed)
            ]
            await asyncio.to_thread(self._client.upsert, self._collection, points)
        # 内容未变但元数据变化的点：仅 merge 更新 meta 字段，避免重复 embedding
        embedded = set(to_embed)
        for i in range(len(texts)):
            if i in embedded:
                continue
            if existing.get(point_ids[i], {}).get("meta") != metas[i]:
                await asyncio.to_thread(
                    self._client.set_payload,
                    self._collection,
                    {"meta": metas[i]},
                    [point_ids[i]],
                )
        stale = set(existing) - set(point_ids)
        if stale:
            await asyncio.to_thread(self._client.delete, self._collection, list(stale))
        self._idx_map = {point_ids[i]: i for i in range(len(ids))}
        self._metas = metas

    # 检索与 query 最相似的 top_k 个点，把 point id 映射回 texts 下标返回；
    # filter 可下推时由服务端剪枝，无法下推的字段改为白名单，客户端召回后二次筛选
    async def search(
        self,
        query: str,
        top_k: int,
        allowed: set[int] | None = None,
        filter: dict[str, object] | None = None,
    ) -> list[tuple[int, float]]:
        if not self._idx_map or not query.strip():
            return []
        if allowed is not None and not allowed:
            return []
        qvec = (await self._embed._embed([query]))[0]
        # 拆解条件：能表达为 Qdrant 条件的下推，不能表达的（字符串范围/列表元数据）转为白名单
        pushed_cond, local_cond = self._split_filter(filter)
        allowed = _intersect(allowed, self._local_allowed(local_cond))
        if allowed is not None and not allowed:
            return []
        # 未下推任何条件时按白名单规模全量召回，避免服务端返回的 top_k 中有效候选不足
        limit = top_k if pushed_cond else max(top_k, len(allowed or self._idx_map))
        resp = await asyncio.to_thread(
            self._client.query_points,
            collection_name=self._collection,
            query=qvec,
            limit=limit,
            query_filter=self._build_filter(pushed_cond),
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

    # 把过滤条件拆为 (可下推给 Qdrant 的部分, 需本地筛选的部分)；
    # 非数值范围无法用 Range 表达，含列表元数据的字段 Qdrant 语义与本地不一致，均回落本地
    def _split_filter(
        self, cond: dict[str, object] | None
    ) -> tuple[dict[str, object], dict[str, object]]:
        if not cond:
            return {}, {}
        pushed: dict[str, object] = {}
        local: dict[str, object] = {}
        for key, val in cond.items():
            if is_range(val) and not is_numeric_range(cast(dict[str, object], val)):
                local[key] = val  # 如 updated_at 的 "2026-03"，Range 只接受数值
            elif _has_list_meta(self._metas, key):
                local[key] = val  # 字段本身是标签列表，改由本地按包含语义匹配
            else:
                pushed[key] = val
        return pushed, local

    # 把本地筛选条件解析为允许下标集合；无条件时返回 None 表示不限制
    def _local_allowed(self, cond: dict[str, object]) -> set[int] | None:
        if not cond:
            return None
        return select(self._metas, cond)

    # 把键值过滤条件转成 Qdrant 服务端 Filter；单值精确、列表任一、字典为数值范围
    def _build_filter(self, cond: dict[str, object] | None) -> Any:
        if not cond:
            return None
        must: list[Any] = []
        for key, val in cond.items():
            # 可过滤字段统一存于 payload.meta 命名空间下
            field = f"meta.{key}"
            if is_range(val):
                must.append(
                    self._models.FieldCondition(
                        key=field,
                        range=self._build_range(cast(dict[str, object], val)),
                    )
                )
                continue
            match: Any
            if isinstance(val, list):
                match = self._models.MatchAny(any=[str(v) for v in val])
            else:
                match = self._models.MatchValue(value=str(val))
            must.append(self._models.FieldCondition(key=field, match=match))
        return self._models.Filter(must=must)

    # 把范围条件转成 Qdrant Range；调用前已确认边界全为数值，直接透传
    def _build_range(self, cond: dict[str, object]) -> Any:
        kwargs: dict[str, Any] = {}
        for op in ("gt", "gte", "lt", "lte"):
            if op in cond:
                kwargs[op] = cond[op]
        return self._models.Range(**kwargs)


# 求两个允许集合的交集，None 表示不限制
def _intersect(a: set[int] | None, b: set[int] | None) -> set[int] | None:
    if a is None:
        return b
    return a if b is None else a & b


# 判断元数据索引中某字段是否存在列表取值（列表字段不参与 Qdrant 服务端过滤）
def _has_list_meta(metas: list[dict[str, Any]], key: str) -> bool:
    return any(isinstance(m.get(key), list) for m in metas)
