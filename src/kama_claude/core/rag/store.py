from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kama_claude.core.rag.chunk import extract_topics, split_semantic
from kama_claude.core.rag.index import Bm25Index, RagIndex, hash_text
from kama_claude.core.rag.rerank import Reranker

# 语义切片算法版本；改动切片逻辑时递增，使旧清单/缓存失效
_CHUNK_VERSION = "2"
# 增量清单结构版本；改动清单字段时递增，使旧清单失效并全量重建
_MANIFEST_VERSION = 1

log = logging.getLogger(__name__)


@dataclass
class RagChunk:
    id: str  # 稳定 id："{doc_id}::{序号}"
    source: str  # 源文件绝对路径
    doc_id: str  # 关联文档标识（相对知识库根目录路径），供按文档聚合/删除
    text: str
    content_hash: str  # chunk 文本的 sha256，供向量缓存按内容复用
    topic: str = ""  # 所属主题标签（文档级），供过滤检索与清单展示


class RagStore:
    # 初始化分块参数与检索后端；index 缺省为本地 BM25
    def __init__(
        self,
        *,
        chunk_size: int = 800,
        chunk_overlap: int = 100,
        index: RagIndex | None = None,
        reranker: Reranker | None = None,
        rerank_depth: int = 50,
    ) -> None:
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._index = index or Bm25Index()
        self._reranker = reranker
        self._rerank_depth = rerank_depth
        self._chunks: list[RagChunk] = []
        self._manifest_path: Path | None = None
        self._topics: list[str] = []  # 全局主题清单（去重、保序），供系统提示注入

    # 递归加载目录下的文本文件，按内容 hash 增量重建索引，返回 chunk 数
    async def load_dir(
        self, directory: Path, exts: tuple[str, ...] = (".md", ".txt")
    ) -> int:
        directory = Path(directory)
        if not directory.is_dir():
            return 0

        files: list[Path] = []
        for ext in exts:
            files.extend(directory.rglob(f"*{ext}"))
        files = sorted(set(files))

        self._manifest_path = directory / ".rag_index_manifest.json"
        prev_docs = self._load_manifest()

        # 当前目录文件：相对路径(doc_id) -> 绝对路径，用于按文档聚合
        current: dict[str, Path] = {
            f.relative_to(directory).as_posix(): f for f in files
        }

        chunks: list[RagChunk] = []
        next_docs: dict[str, dict[str, Any]] = {}
        topics: list[str] = []

        for doc_id in sorted(current):
            f = current[doc_id]
            raw = self._read(f)
            if raw is None:
                continue  # 读取失败：丢弃该文档，下次再试
            content_hash = hash_text(raw)
            prev = prev_docs.get(doc_id)
            # 内容 hash 未变 → 复用持久化 chunk，避免重复切分；否则重新切块
            if prev is not None and content_hash == prev.get("content_hash"):
                texts = self._reuse_chunks(prev) or split_semantic(
                    raw, self._chunk_size, self._chunk_overlap
                )
            else:
                texts = split_semantic(raw, self._chunk_size, self._chunk_overlap)
            chunks.extend(self._chunks_from(doc_id, str(f), texts))
            # 提取文档级主题：取首个标题作为归属主题，其余标题并入全局清单
            doc_topics = extract_topics(raw)
            doc_topic = doc_topics[0] if doc_topics else ""
            next_docs[doc_id] = {
                "path": str(f),
                "content_hash": content_hash,
                "chunks": texts,
                "topic": doc_topic,
            }
            topics.extend(doc_topics)

        # 用文档主题回填各 chunk 的 topic 标签（chunk 在 _chunks_from 时尚未知主题）
        for c in chunks:
            c.topic = next_docs[c.doc_id].get("topic", "")

        self._chunks = chunks
        self._topics = _dedupe(topics)
        self._save_manifest(next_docs)
        await self._index.build(
            [c.text for c in chunks], [c.id for c in chunks], self._build_token()
        )
        return len(chunks)

    # 检索与 query 最相似的 top_k 个 chunk；topic 非空时先按主题过滤候选集
    async def search(self, query: str, top_k: int = 3, topic: str = "") -> list[RagChunk]:
        if not self._chunks or not query.strip():
            return []
        # 主题过滤：命中 chunk 的下标白名单，供索引层只在这些下标内打分
        allowed: set[int] | None = None
        if topic:
            allowed = {i for i, c in enumerate(self._chunks) if c.topic == topic}
            if not allowed:
                return []
        if self._reranker is None:
            scores = await self._index.search(query, top_k, allowed)
            return [self._chunks[i] for i, _ in scores]
        depth = max(top_k, min(self._rerank_depth, 64))
        scores = await self._index.search(query, depth, allowed)
        candidates = [self._chunks[i] for i, _ in scores]
        r_scores = await self._reranker.rerank(query, [c.text for c in candidates])
        ranked = sorted(zip(candidates, r_scores), key=lambda x: x[1], reverse=True)
        return [c for c, _ in ranked[:top_k]]

    # 返回知识库主题清单的纯文本，供注入 System Prompt 或工具描述
    def manifest(self) -> str:
        if not self._topics:
            return ""
        return "知识库当前覆盖主题：" + "、".join(self._topics)

    # 用文本列表组装 RagChunk，补上稳定 id、doc_id 与内容 hash
    def _chunks_from(self, doc_id: str, source: str, texts: list[str]) -> list[RagChunk]:
        return [
            RagChunk(
                id=f"{doc_id}::{i}",
                source=source,
                doc_id=doc_id,
                text=text,
                content_hash=hash_text(text),
            )
            for i, text in enumerate(texts)
        ]

    # 从清单记录取出可复用的 chunk 文本；记录损坏时返回 None 触发重切
    def _reuse_chunks(self, prev: dict[str, Any]) -> list[str] | None:
        chunks = prev.get("chunks")
        if isinstance(chunks, list) and all(isinstance(c, str) for c in chunks):
            return chunks
        return None

    # 读取文件内容；IO 错误返回 None
    def _read(self, f: Path) -> str | None:
        try:
            return f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    # 生成传给后端的构建令牌；后端以 chunk 内容 hash 为准，此令牌仅作兜底标识
    def _build_token(self) -> str:
        return f"{_CHUNK_VERSION}:{self._chunk_size}:{self._chunk_overlap}"

    # 读取增量清单；版本或分块参数不符、或结构损坏时返回空，触发全量重建
    def _load_manifest(self) -> dict[str, dict[str, Any]]:
        if self._manifest_path is None:
            return {}
        try:
            data = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if (
            data.get("version") != _MANIFEST_VERSION
            or data.get("chunk_version") != _CHUNK_VERSION
            or data.get("chunk_size") != self._chunk_size
            or data.get("chunk_overlap") != self._chunk_overlap
        ):
            return {}
        docs = data.get("docs")
        if not isinstance(docs, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for key, rec in docs.items():
            if isinstance(key, str) and isinstance(rec, dict):
                out[key] = rec
        return out

    # 将每个文档的元信息与 chunk 文本落盘，供下次增量加载复用
    def _save_manifest(self, docs: dict[str, dict[str, Any]]) -> None:
        if self._manifest_path is None:
            return
        payload = {
            "version": _MANIFEST_VERSION,
            "chunk_version": _CHUNK_VERSION,
            "chunk_size": self._chunk_size,
            "chunk_overlap": self._chunk_overlap,
            "docs": docs,
        }
        try:
            self._manifest_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            log.warning("failed to write RAG manifest: %s", self._manifest_path)


# 去除空白项并按首次出现顺序去重，用于聚合主题清单
def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        s = item.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out
