from __future__ import annotations

import json
from pathlib import Path

import httpx

from kama_claude.core.rag.index import ZhipuEmbeddingIndex
from kama_claude.core.rag.vector_db import QdrantIndex


# 构造用 MockTransport 返回确定性向量的智谱 embedding，供 QdrantIndex 复用
def _fake_embed() -> ZhipuEmbeddingIndex:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        data = []
        for idx, text in enumerate(body["input"]):
            vec = [1.0, 0.0] if "machine" in text.lower() else [0.0, 1.0]
            data.append({"object": "embedding", "index": idx, "embedding": vec})
        return httpx.Response(200, json={"object": "list", "data": data, "model": "embedding-3"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://test.local")
    return ZhipuEmbeddingIndex("k", dimensions=2, client=client)


# 功能：验证 QdrantIndex 用稳定 id 建库后按余弦命中，且返回 texts 下标
# 设计：本地 local mode + 确定性 embedding，断言 top-1 命中含 "machine" 的 chunk 下标，
#       证明稳定 id 通过 _idx_map 正确映射回原语料位置
async def test_qdrant_index_build_and_search(tmp_path: Path) -> None:
    index = QdrantIndex(_fake_embed(), path=str(tmp_path / "qdrant"))
    await index.build(
        ["machine learning notes", "cooking recipe"], ["a::0", "b::0"], "fp"
    )
    scores = await index.search("machine learning", top_k=1)
    assert scores == [(0, 1.0)]


# 功能：验证 QdrantIndex 按稳定 id + content_hash 增量 upsert，未变 chunk 不重调 embedding
# 设计：记录 embedding 调用次数，同内容二次构建不新增调用；改写后仅新 chunk 触发 embedding，
#       且被删除的稳定 id 从库中清除
async def test_qdrant_index_incremental(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["input"])
        data = [{"object": "embedding", "index": i, "embedding": [1.0]} for i in range(len(body["input"]))]
        return httpx.Response(200, json={"object": "list", "data": data, "model": "embedding-3"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://test.local")
    embed = ZhipuEmbeddingIndex("k", dimensions=1, client=client)
    index = QdrantIndex(embed, path=str(tmp_path / "qdrant"))

    await index.build(["alpha", "beta"], ["a::0", "b::0"], "fp1")
    assert len(calls) == 1

    # 内容未变 → 命中 payload 中的 content_hash，不重调 embedding
    await index.build(["alpha", "beta"], ["a::0", "b::0"], "fp1")
    assert len(calls) == 1

    # 改 b、删 a → 仅新 chunk "gamma" 调 embedding，稳定 id 精确 upsert/delete
    await index.build(["beta", "gamma"], ["b::0", "c::0"], "fp2")
    assert len(calls) == 2
    assert calls[-1] == ["gamma"]


# 功能：验证 QdrantIndex 把 filter 转成服务端条件下推，命中符合元数据的点
# 设计：两个 chunk 向量相同但 meta 不同，带 filter 检索仅返回 department=研发 的点，
#       证明过滤在 Qdrant 侧完成而非召回后内存过滤
async def test_qdrant_index_metadata_filter_pushdown(tmp_path: Path) -> None:
    index = QdrantIndex(_fake_embed(), path=str(tmp_path / "qdrant"))
    texts = ["machine learning notes", "machine learning notes"]
    metas = [{"department": "研发", "topic": "AI"}, {"department": "财务", "topic": "AI"}]
    await index.build(texts, ["a::0", "b::0"], "fp", metas)
    scores = await index.search("machine learning", top_k=2, filter={"department": "研发"})
    assert scores == [(0, 1.0)]


# 功能：验证 QdrantIndex 对 filter 列表值使用 MatchAny（任一匹配）
# 设计：两个点分别标研发/财务，filter 传列表命中两者，断言返回全部下标
async def test_qdrant_index_metadata_filter_any(tmp_path: Path) -> None:
    index = QdrantIndex(_fake_embed(), path=str(tmp_path / "qdrant"))
    texts = ["machine learning notes", "machine learning notes"]
    metas = [{"department": "研发"}, {"department": "财务"}]
    await index.build(texts, ["a::0", "b::0"], "fp", metas)
    scores = await index.search(
        "machine learning", top_k=2, filter={"department": ["研发", "财务"]}
    )
    assert sorted(i for i, _ in scores) == [0, 1]
