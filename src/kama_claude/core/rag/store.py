from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

# 中文汉字区间，用于构造字符 bigram 分词
_HAN_RE = re.compile(r"[一-鿿]")
# 英文/数字单词，用于英文分词
_WORD_RE = re.compile(r"[a-z0-9]+")


@dataclass
class RagChunk:
    id: str
    source: str
    text: str


# 将文本切分为英文单词 + 中文字符 bigram 的 token 列表
def _tokenize(text: str) -> list[str]:
    lowered = text.lower()
    tokens = _WORD_RE.findall(lowered)
    han = _HAN_RE.findall(lowered)
    for i in range(len(han) - 1):
        tokens.append(han[i] + han[i + 1])
    return tokens


# 将长文本按固定大小切块，带固定重叠避免截断语义
def _split_chunks(text: str, size: int, overlap: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    step = max(1, size - overlap)
    chunks: list[str] = []
    start = 0
    while start < len(text):
        chunks.append(text[start:start + size])
        start += step
    return chunks


class RagStore:
    # 初始化空索引与分块参数
    def __init__(self, chunk_size: int = 800, chunk_overlap: int = 100) -> None:
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._chunks: list[RagChunk] = []
        self._doc_vectors: list[dict[str, float]] = []
        self._idf: dict[str, float] = {}

    # 递归加载目录下的文本文件，切块并构建 TF-IDF 索引，返回 chunk 数
    def load_dir(self, directory: Path, exts: tuple[str, ...] = (".md", ".txt")) -> int:
        directory = Path(directory)
        if not directory.is_dir():
            return 0

        files: list[Path] = []
        for ext in exts:
            files.extend(directory.rglob(f"*{ext}"))
        files = sorted(set(files))

        chunks: list[RagChunk] = []
        for f in files:
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for i, piece in enumerate(_split_chunks(text, self._chunk_size, self._chunk_overlap)):
                chunks.append(RagChunk(id=f"{f}::{i}", source=str(f), text=piece))

        self._chunks = chunks
        self._build_index()
        return len(chunks)

    # 基于全部 chunk 构建 TF-IDF 向量与 IDF 表
    def _build_index(self) -> None:
        n = len(self._chunks)
        if n == 0:
            self._doc_vectors = []
            self._idf = {}
            return

        per_doc: list[dict[str, int]] = []
        df: dict[str, int] = {}
        for chunk in self._chunks:
            counts: dict[str, int] = {}
            for tok in _tokenize(chunk.text):
                counts[tok] = counts.get(tok, 0) + 1
            per_doc.append(counts)
            for tok in counts:
                df[tok] = df.get(tok, 0) + 1

        # 平滑 IDF：idf = log((N+1)/(df+1)) + 1
        self._idf = {
            tok: math.log((n + 1) / (c + 1)) + 1.0
            for tok, c in df.items()
        }

        # TF-IDF 向量：tf 按 chunk 长度归一化后乘 idf
        self._doc_vectors = []
        for counts in per_doc:
            total = sum(counts.values()) or 1
            vec = {
                tok: (cnt / total) * self._idf[tok]
                for tok, cnt in counts.items()
            }
            self._doc_vectors.append(vec)

    # 检索与 query 最相似的 top_k 个 chunk，返回 RagChunk 列表
    def search(self, query: str, top_k: int = 3) -> list[RagChunk]:
        if not self._chunks or not query.strip():
            return []

        qcounts: dict[str, int] = {}
        for tok in _tokenize(query):
            qcounts[tok] = qcounts.get(tok, 0) + 1
        qtotal = sum(qcounts.values()) or 1
        qvec = {
            tok: (cnt / qtotal) * self._idf.get(tok, 0.0)
            for tok, cnt in qcounts.items()
        }
        if not qvec:
            return []

        scores: list[tuple[float, int]] = []
        for i, dvec in enumerate(self._doc_vectors):
            sim = self._cosine(qvec, dvec)
            if sim > 0.0:
                scores.append((sim, i))
        scores.sort(key=lambda x: -x[0])
        return [self._chunks[i] for _, i in scores[:top_k]]

    # 计算两个稀疏向量的余弦相似度
    def _cosine(self, a: dict[str, float], b: dict[str, float]) -> float:
        common = set(a) & set(b)
        dot = sum(a[k] * b[k] for k in common)
        if dot == 0.0:
            return 0.0
        na = math.sqrt(sum(v * v for v in a.values()))
        nb = math.sqrt(sum(v * v for v in b.values()))
        if na == 0.0 or nb == 0.0:
            return 0.0
        return dot / (na * nb)
