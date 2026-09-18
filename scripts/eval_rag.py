"""RAG 效果评估脚本：构建知识库索引，用一组语义查询验证检索质量。

用法（在 WSL 中，项目根目录下）：
    uv run python scripts/eval_rag.py
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from kama_claude.core.config import get_config
from kama_claude.core.rag.index import Bm25Index, HybridIndex, ZhipuEmbeddingIndex
from kama_claude.core.rag.store import RagStore

# 评估查询集：故意混入同义/改述问法，验证语义检索（TF-IDF 只能命中字面）
QUERIES = [
    "项目的守护进程和客户端之间是怎么通信的？",
    "怎么跑单元测试？",
    "IPC 消息用什么定义的？",
    "哪个界面是主要前端，TUI 还是 CLI？",
    "配置文件有几层优先级？",
    "知识库的切片策略是什么？",
]


# 打印单条查询的检索结果（top-k 分数 + 来源 + 摘要）
def _print_hits(query: str, hits: list[tuple[str, float, str]]) -> None:
    print(f"\n查询：{query}")
    if not hits:
        print("  （无结果）")
        return
    for rank, (src, score, snippet) in enumerate(hits, 1):
        text = snippet.replace("\n", " ")
        if len(text) > 120:
            text = text[:120] + "…"
        print(f"  #{rank}  {score:.4f}  {Path(src).name}\n      {text}")


async def _main() -> None:
    rag = get_config().rag
    # 配了 key 走混合检索（BM25 + embedding），否则纯 BM25 零依赖回退
    if rag.api_key:
        dense = ZhipuEmbeddingIndex(
            api_key=rag.api_key,
            model=rag.embedding_model,
            base_url=rag.embedding_base_url,
            dimensions=rag.embedding_dimensions,
            batch_size=rag.embedding_batch_size,
            cache_path=Path(rag.knowledge_dir).expanduser() / ".rag_index_cache.json",
            cache_enabled=rag.index_cache,
        )
        index = HybridIndex(Bm25Index(), dense)
        backend = f"hybrid (BM25 + Zhipu embedding-3 {rag.embedding_dimensions}d)"
    else:
        index = Bm25Index()
        backend = "BM25（未配置 API key 回退）"

    store = RagStore(
        chunk_size=rag.chunk_size,
        chunk_overlap=rag.chunk_overlap,
        index=index,
    )
    n = await store.load_dir(Path(rag.knowledge_dir).expanduser())
    print(f"后端：{backend}")
    print(f"知识库：{rag.knowledge_dir}  →  {n} 个 chunk")

    # 直接走 index 拿 (chunk 下标, 分数)，便于观察区分度；store.search 只透出排序结果
    for query in QUERIES:
        scores = await store._index.search(query, top_k=rag.top_k)
        hits = [
            (store._chunks[i].source, score, store._chunks[i].text.strip())
            for i, score in scores
        ]
        _print_hits(query, hits)


if __name__ == "__main__":
    asyncio.run(_main())
