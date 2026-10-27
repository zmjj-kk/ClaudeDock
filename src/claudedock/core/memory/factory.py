from __future__ import annotations

from pathlib import Path

from claudedock.core.config import MemoryConfig, RagConfig
from claudedock.core.memory.sqlite_store import MemorySqliteStore
from claudedock.core.memory.store import MemoryStore
from claudedock.core.memory.vector_store import MemoryVectorStore
from claudedock.core.rag.index import Bm25Index, HybridIndex, RagIndex, ZhipuEmbeddingIndex

# 记忆向量缓存的独立文件名，避免与知识库 .rag_index_cache.json 相互覆盖
_MEMORY_CACHE_NAME = ".memory_index_cache.json"


# 构建智谱 embedding 后端；与 rag.factory._dense 相同但使用记忆独立缓存路径
def _dense(rag: RagConfig, cache_path: Path) -> RagIndex:
    zhipu = ZhipuEmbeddingIndex(
        api_key=rag.api_key,
        model=rag.embedding_model,
        base_url=rag.embedding_base_url,
        dimensions=rag.embedding_dimensions,
        batch_size=rag.embedding_batch_size,
        cache_path=cache_path,
        cache_enabled=rag.index_cache,
    )
    # 配置了向量数据库时，用它接管存储与检索，并使用记忆独立集合
    if rag.vector_db == "qdrant":
        from claudedock.core.rag.vector_db import QdrantIndex

        return QdrantIndex(
            zhipu,
            url=rag.qdrant_url,
            path=rag.qdrant_path,
            collection="claudedock_memories",
        )
    return zhipu


# 按 rag 配置构建记忆用检索后端：复用 BM25/embedding/hybrid，无 key 时退回本地 BM25
def build_memory_index(rag: RagConfig, *, cache_path: Path) -> RagIndex:
    mode = rag.retriever
    if mode == "bm25":
        return Bm25Index()
    if mode == "embedding":
        return _dense(rag, cache_path)
    if mode == "hybrid":
        return HybridIndex(Bm25Index(), _dense(rag, cache_path))
    # auto：有 key 走 hybrid，否则纯 BM25 零依赖回退
    return HybridIndex(Bm25Index(), _dense(rag, cache_path)) if rag.api_key else Bm25Index()


# 构建未加载的 MemoryStore：sqlite 权威层 + 向量派生层（复用 RAG 后端，独立缓存/集合）
def build_memory_store(memory: MemoryConfig, rag: RagConfig) -> MemoryStore:
    store_dir = Path(memory.store_dir).expanduser()
    sqlite_store = MemorySqliteStore(store_dir / "memories.db")
    cache_path = store_dir / _MEMORY_CACHE_NAME
    index = build_memory_index(rag, cache_path=cache_path)
    vector_store = MemoryVectorStore(
        index, chunk_size=rag.chunk_size, chunk_overlap=rag.chunk_overlap
    )
    return MemoryStore(memory, sqlite_store=sqlite_store, vector_store=vector_store)
