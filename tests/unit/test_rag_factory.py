from __future__ import annotations

from kama_claude.core.config import RagConfig
from kama_claude.core.rag.factory import _build_reranker, build_index, build_store
from kama_claude.core.rag.index import Bm25Index, HybridIndex, ZhipuEmbeddingIndex
from kama_claude.core.rag.rerank import ZhipuReranker


# 功能：验证 build_index 按 retriever 显式指定返回对应后端类型
# 设计：bm25/embedding/hybrid 三分支逐一断言返回类型，覆盖工厂的显式选择路径
def test_build_index_explicit_retriever() -> None:
    rag = RagConfig()
    assert isinstance(build_index(rag, retriever="bm25"), Bm25Index)
    assert isinstance(build_index(rag, retriever="embedding"), ZhipuEmbeddingIndex)
    assert isinstance(build_index(rag, retriever="hybrid"), HybridIndex)


# 功能：验证 auto 模式按是否配置 embedding key 在 hybrid 与 bm25 间回退
# 设计：无 key 走零依赖 BM25，有 key 走混合检索，覆盖默认回退路径
def test_build_index_auto_fallback() -> None:
    assert isinstance(build_index(RagConfig()), Bm25Index)
    assert isinstance(build_index(RagConfig(api_key="k")), HybridIndex)


# 功能：验证 build_store 返回绑定选定后端与分块参数的 RagStore
# 设计：断言 store 内部索引类型与 chunk_size/overlap 透传，确认工厂封装完整
def test_build_store() -> None:
    rag = RagConfig(chunk_size=512, chunk_overlap=64)
    store = build_store(rag, retriever="bm25")
    assert isinstance(store._index, Bm25Index)
    assert store._chunk_size == 512
    assert store._chunk_overlap == 64


# 功能：验证 _build_reranker 在无 key 时返回 None、有 key 时返回 ZhipuReranker
# 设计：直接调用私有工厂，覆盖 rerank 依赖远程 key 的门控逻辑
def test_build_reranker_gated_by_key() -> None:
    assert _build_reranker(RagConfig()) is None
    assert isinstance(_build_reranker(RagConfig(api_key="k")), ZhipuReranker)


# 功能：验证 rerank_enabled=True 时 build_store 绑定 ZhipuReranker 并透传 depth
# 设计：配 api_key + rerank_enabled + 自定义 depth，断言 store 内部 reranker 类型与 depth
def test_build_store_with_rerank() -> None:
    rag = RagConfig(api_key="k", rerank_enabled=True, rerank_depth=30)
    store = build_store(rag, retriever="bm25")
    assert isinstance(store._reranker, ZhipuReranker)
    assert store._rerank_depth == 30


# 功能：验证 rerank=False 覆盖参数可强制关闭 rerank（供评测 A/B）
# 设计：即使 rerank_enabled=True，显式 rerank=False 时 store._reranker 为 None
def test_build_store_rerank_override_off() -> None:
    rag = RagConfig(api_key="k", rerank_enabled=True)
    store = build_store(rag, retriever="bm25", rerank=False)
    assert store._reranker is None


# 功能：验证无 api_key 时即使开启 rerank 也不绑定 reranker（依赖远程 API）
# 设计：api_key 为空 + rerank_enabled=True，断言 store._reranker 为 None，安全降级为单阶段
def test_build_store_rerank_no_key() -> None:
    rag = RagConfig(rerank_enabled=True)
    store = build_store(rag, retriever="bm25")
    assert store._reranker is None
