from __future__ import annotations

import hashlib
from pathlib import Path

from claudedock.core.config import RagConfig
from claudedock.core.llm.base import LLMProvider
from claudedock.core.rag.index import Bm25Index, HybridIndex, RagIndex, ZhipuEmbeddingIndex
from claudedock.core.rag.query import LlmQueryProcessor
from claudedock.core.rag.rerank import ZhipuReranker
from claudedock.core.rag.store import RagStore


# 构建智谱 embedding 检索后端，供 hybrid / embedding 两种模式复用
def _dense(rag: RagConfig) -> RagIndex:
    zhipu = ZhipuEmbeddingIndex(
        api_key=rag.api_key,
        model=rag.embedding_model,
        base_url=rag.embedding_base_url,
        dimensions=rag.embedding_dimensions,
        batch_size=rag.embedding_batch_size,
        cache_path=Path(rag.knowledge_dir).expanduser() / ".rag_index_cache.json",
        cache_enabled=rag.index_cache,
    )
    # 配置了向量数据库时，用它接管存储与检索；embedding 仍由智谱生成
    if rag.vector_db == "qdrant":
        from claudedock.core.rag.vector_db import QdrantIndex

        namespace = hashlib.sha256(
            str(Path(rag.knowledge_dir).expanduser().resolve()).encode()
        ).hexdigest()[:16]
        return QdrantIndex(
            zhipu,
            url=rag.qdrant_url,
            path=rag.qdrant_path,
            collection=f"claudedock_chunks_{namespace}",
        )
    return zhipu


# 按 retriever（可覆盖）选检索后端；auto 时配 key 走 hybrid，否则纯 BM25 零依赖回退
def build_index(rag: RagConfig, *, retriever: str | None = None) -> RagIndex:
    mode = retriever or rag.retriever
    if mode == "bm25":
        return Bm25Index()
    if mode == "embedding":
        return _dense(rag)
    if mode == "hybrid":
        return HybridIndex(Bm25Index(), _dense(rag))
    return HybridIndex(Bm25Index(), _dense(rag)) if rag.api_key else Bm25Index()


# 构建智谱 reranker；未配置 key 时返回 None（rerank 依赖远程 API）
def _build_reranker(rag: RagConfig) -> ZhipuReranker | None:
    if not rag.api_key:
        return None
    return ZhipuReranker(
        api_key=rag.api_key,
        model=rag.rerank_model,
        base_url=rag.embedding_base_url,
    )


# 构建未加载的 RagStore（绑定选定后端与分块参数），rerank/query_enhance 覆盖参数供评测 A/B 用
def build_store(
    rag: RagConfig,
    *,
    retriever: str | None = None,
    rerank: bool | None = None,
    provider: LLMProvider | None = None,
    query_enhance: bool | None = None,
) -> RagStore:
    use_rerank = rag.rerank_enabled if rerank is None else rerank
    use_enhance = rag.query_enhance_enabled if query_enhance is None else query_enhance
    processor = None
    if use_enhance and provider is not None:
        processor = LlmQueryProcessor(provider, expand_n=rag.query_expand_n)
    return RagStore(
        chunk_size=rag.chunk_size,
        chunk_overlap=rag.chunk_overlap,
        index=build_index(rag, retriever=retriever),
        reranker=_build_reranker(rag) if use_rerank else None,
        rerank_depth=rag.rerank_depth,
        query_processor=processor,
        query_depth=rag.query_depth,
    )
