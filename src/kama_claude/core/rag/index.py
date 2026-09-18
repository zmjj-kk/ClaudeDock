from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from abc import ABC, abstractmethod
from pathlib import Path

import httpx

# 中文汉字区间，用于构造字符 bigram 分词
_HAN_RE = re.compile(r"[一-鿿]")
# 英文/数字单词，用于英文分词
_WORD_RE = re.compile(r"[a-z0-9]+")

# embedding 网络请求的重试退避秒数（连接/读中断时重试）
_RETRY_BACKOFF_S = (1.0, 2.0)
_MAX_RETRIES = 2

log = logging.getLogger(__name__)


# 将文本切分为英文单词 + 中文字符 bigram 的 token 列表
def _tokenize(text: str) -> list[str]:
    lowered = text.lower()
    tokens = _WORD_RE.findall(lowered)
    han = _HAN_RE.findall(lowered)
    for i in range(len(han) - 1):
        tokens.append(han[i] + han[i + 1])
    return tokens


# 计算两个稠密向量的余弦相似度
def _cosine_dense(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    if dot == 0.0:
        return 0.0
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# 计算文本的 sha256 内容指纹，作为 chunk 内容寻址的缓存键
def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RagIndex(ABC):
    # 基于语料构建索引；fingerprint 用于缓存校验
    @abstractmethod
    async def build(self, texts: list[str], fingerprint: str) -> None: ...

    # 检索 top_k 个最相关 chunk，返回 (chunk 下标, 分数) 列表
    @abstractmethod
    async def search(self, query: str, top_k: int) -> list[tuple[int, float]]: ...


class Bm25Index(RagIndex):
    # 初始化 BM25 参数：k1 词频饱和、b 文档长度归一化
    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self._k1 = k1
        self._b = b
        self._n = 0
        self._df: dict[str, int] = {}
        self._doc_tf: list[dict[str, int]] = []
        self._doc_len: list[int] = []
        self._avgdl = 0.0

    # 统计每篇文档词频、文档长度与全局 df，并计算平均文档长度
    async def build(self, texts: list[str], fingerprint: str) -> None:
        del fingerprint
        self._n = len(texts)
        self._df = {}
        self._doc_tf = []
        self._doc_len = []
        for text in texts:
            counts: dict[str, int] = {}
            for tok in _tokenize(text):
                counts[tok] = counts.get(tok, 0) + 1
            self._doc_tf.append(counts)
            self._doc_len.append(sum(counts.values()) or 1)
            for tok in counts:
                self._df[tok] = self._df.get(tok, 0) + 1
        self._avgdl = sum(self._doc_len) / self._n if self._n else 0.0

    # BM25 IDF：log(1 + (N - df + 0.5) / (df + 0.5))
    def _idf(self, tok: str) -> float:
        df = self._df.get(tok, 0)
        return math.log(1 + (self._n - df + 0.5) / (df + 0.5))

    # 对每篇文档累加查询词项的 BM25 得分，按分数取 top_k
    async def search(self, query: str, top_k: int) -> list[tuple[int, float]]:
        if self._n == 0 or not query.strip():
            return []
        q_tokens = set(_tokenize(query))
        scores: list[tuple[float, int]] = []
        for i in range(self._n):
            tf = self._doc_tf[i]
            dl = self._doc_len[i]
            s = 0.0
            for t in q_tokens:
                f = tf.get(t)
                if f:
                    num = f * (self._k1 + 1)
                    den = f + self._k1 * (1 - self._b + self._b * dl / self._avgdl)
                    s += self._idf(t) * num / den
            if s > 0.0:
                scores.append((s, i))
        scores.sort(key=lambda x: -x[0])
        return [(i, s) for s, i in scores[:top_k]]


class ZhipuEmbeddingIndex(RagIndex):
    # 初始化智谱 embedding 客户端与缓存参数；client 可在测试时注入
    def __init__(
        self,
        api_key: str,
        model: str = "embedding-3",
        base_url: str = "https://open.bigmodel.cn/api/paas/v4",
        dimensions: int = 2048,
        batch_size: int = 64,
        cache_path: Path | None = None,
        cache_enabled: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._dimensions = dimensions
        self._batch_size = max(1, batch_size)
        self._cache_path = cache_path
        self._cache_enabled = cache_enabled
        self._vectors: list[list[float]] = []
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    # 构建稠密向量索引：按 chunk 内容 hash 复用缓存，仅对新增/变更 chunk 调用 API
    async def build(self, texts: list[str], fingerprint: str) -> None:
        del fingerprint
        if not texts:
            self._vectors = []
            return
        cached = self._cache_enabled and self._cache_path is not None
        entries = self._load_cache() if cached else {}
        hashes = [hash_text(t) for t in texts]
        vectors: list[list[float] | None] = [entries.get(h) for h in hashes]
        missing = [i for i, v in enumerate(vectors) if v is None]
        if missing:
            embedded = await self._embed([texts[i] for i in missing])
            for i, vec in zip(missing, embedded):
                vectors[i] = vec
                entries[hashes[i]] = vec
        if cached and missing:
            # 只保留当前语料仍存在的 chunk，清理已删除/变更文档的陈旧向量
            self._save_cache({h: entries[h] for h in hashes})
        self._vectors = [v for v in vectors if v is not None]

    # 检索与 query 最相似的 top_k 个 chunk 下标与分数
    async def search(self, query: str, top_k: int) -> list[tuple[int, float]]:
        if not self._vectors or not query.strip():
            return []
        qvec = (await self._embed([query]))[0]
        scores: list[tuple[float, int]] = []
        for i, dvec in enumerate(self._vectors):
            sim = _cosine_dense(qvec, dvec)
            if sim > 0.0:
                scores.append((sim, i))
        scores.sort(key=lambda x: -x[0])
        return [(i, s) for s, i in scores[:top_k]]

    # 分批调用 embedding API，返回与输入顺序一致的向量列表
    async def _embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            vectors.extend(await self._embed_batch(texts[start : start + self._batch_size]))
        return vectors

    # 单批 embedding 请求，连接/读中断时指数退避重试
    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        payload = {"model": self._model, "input": batch, "dimensions": self._dimensions}
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await self._client.post("/embeddings", json=payload)
                resp.raise_for_status()
                data = resp.json()
                items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
                return [[float(x) for x in item["embedding"]] for item in items]
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
                if attempt == _MAX_RETRIES:
                    raise
                log.warning(
                    "embedding request failed (attempt %d/%d): %s",
                    attempt + 1, _MAX_RETRIES + 1, exc,
                )
                await asyncio.sleep(_RETRY_BACKOFF_S[attempt])
        raise RuntimeError("unreachable")  # pragma: no cover

    # 读取按内容 hash 组织的向量缓存；模型/维度不符或损坏时返回空
    def _load_cache(self) -> dict[str, list[float]]:
        if self._cache_path is None:
            return {}
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if data.get("model") != self._model or data.get("dimensions") != self._dimensions:
            return {}
        entries = data.get("entries")
        if not isinstance(entries, dict):
            return {}
        out: dict[str, list[float]] = {}
        for key, vec in entries.items():
            if isinstance(key, str) and isinstance(vec, list):
                out[key] = [float(x) for x in vec]
        return out

    # 将内容 hash → 向量的映射落盘；写入失败仅告警，不阻断建索引
    def _save_cache(self, entries: dict[str, list[float]]) -> None:
        if self._cache_path is None:
            return
        payload = {"model": self._model, "dimensions": self._dimensions, "entries": entries}
        try:
            self._cache_path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            log.warning("failed to write RAG index cache: %s", self._cache_path)


class HybridIndex(RagIndex):
    # 组合词法与语义两个子检索器，用 RRF 按排名融合
    def __init__(
        self,
        lexical: RagIndex,
        dense: RagIndex,
        *,
        rrf_k: int = 60,
        depth: int = 100,
    ) -> None:
        self._lexical = lexical
        self._dense = dense
        self._rrf_k = rrf_k
        self._depth = depth

    # 同时构建两个子索引（BM25 重建成本低；embedding 命中已有缓存）
    async def build(self, texts: list[str], fingerprint: str) -> None:
        await self._lexical.build(texts, fingerprint)
        await self._dense.build(texts, fingerprint)

    # 分别取 depth 个候选，按 RRF 分数合并后返回 top_k
    async def search(self, query: str, top_k: int) -> list[tuple[int, float]]:
        lex = await self._lexical.search(query, self._depth)
        den = await self._dense.search(query, self._depth)
        fused: dict[int, float] = {}
        for rank, (idx, _s) in enumerate(lex):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (self._rrf_k + rank + 1)
        for rank, (idx, _s) in enumerate(den):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (self._rrf_k + rank + 1)
        ranked = sorted(fused.items(), key=lambda x: -x[1])
        return [(idx, s) for idx, s in ranked[:top_k]]
