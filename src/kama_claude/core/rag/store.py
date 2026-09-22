from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from kama_claude.core.rag.chunk import extract_topics, parse_frontmatter, split_semantic
from kama_claude.core.rag.filter import MetaValue, canonical, matches
from kama_claude.core.rag.index import Bm25Index, RagIndex, hash_text, rrf_fuse
from kama_claude.core.rag.query import QueryProcessor, optimize_query
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
    topic: str = ""  # 所属主题标签（文档级），仅用于 manifest 清单展示，不作过滤条件
    # 结构化元数据（来源/时间/部门等），值已类型化（str/int/float/bool/list[str]）
    metadata: dict[str, MetaValue] = field(default_factory=dict)


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
        query_processor: QueryProcessor | None = None,
        query_depth: int = 10,
    ) -> None:
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._index = index or Bm25Index()
        self._reranker = reranker
        self._rerank_depth = rerank_depth
        self._query_processor = query_processor
        self._query_depth = query_depth
        self._chunks: list[RagChunk] = []
        self._manifest_path: Path | None = None
        self._topics: list[str] = []  # 全局主题清单（去重、保序），供系统提示注入
        self._inverted: dict[str, dict[str, set[int]]] = {}  # 字段值 → chunk 下标倒排索引

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
            # 结构化元数据：来源路径、更新时间、frontmatter 显式字段
            meta: dict[str, MetaValue] = {"source": doc_id, "updated_at": self._updated_at(f)}
            meta.update(parse_frontmatter(raw))
            next_docs[doc_id] = {
                "path": str(f),
                "content_hash": content_hash,
                "chunks": texts,
                "topic": doc_topic,
                "metadata": meta,
            }
            topics.extend(doc_topics)

        # 用文档主题与元数据回填各 chunk（chunk 在 _chunks_from 时尚未知）
        for c in chunks:
            rec = next_docs[c.doc_id]
            c.topic = rec.get("topic", "")
            c.metadata = dict(rec.get("metadata", {}))

        self._chunks = chunks
        self._topics = _dedupe(topics)
        self._inverted = self._build_inverted(chunks)
        self._save_manifest(next_docs)
        # 元数据直接作为可过滤字段传给后端，供向量库服务端过滤下推
        metas = [dict(c.metadata) for c in chunks]
        await self._index.build(
            [c.text for c in chunks], [c.id for c in chunks], self._build_token(), metas
        )
        return len(chunks)

    # 检索与 query 最相似的 top_k 个 chunk；filter 非空时按元数据过滤候选集
    async def search(
        self,
        query: str,
        top_k: int = 3,
        filter: dict[str, object] | None = None,
    ) -> list[RagChunk]:
        if not self._chunks or not query.strip():
            return []
        # 过滤：翻译成命中 chunk 下标白名单，供内存后端二次筛选
        cond = dict(filter) if filter else {}
        allowed = self._match(cond) if cond else None
        if allowed is not None and not allowed:
            return []
        # 查询优化：统一空白规范化
        base = optimize_query(query)
        # 配置了查询处理器时，改写+拓展为多条子查询；否则单条原始查询
        if self._query_processor is not None:
            queries = await self._query_processor.transform(base)
        else:
            queries = [base]
        # 有 reranker 时先取更深的候选集再精排，否则直接取 top_k
        if self._reranker is not None:
            depth = max(top_k, min(self._rerank_depth, 64))
            scores = await self._retrieve(queries, depth, allowed, cond or None)
            candidates = [self._chunks[i] for i, _ in scores]
            r_scores = await self._reranker.rerank(base, [c.text for c in candidates])
            ranked = sorted(zip(candidates, r_scores), key=lambda x: x[1], reverse=True)
            return [c for c, _ in ranked[:top_k]]
        scores = await self._retrieve(queries, top_k, allowed, cond or None)
        return [self._chunks[i] for i, _ in scores]

    # 对每条子查询检索 limit 个候选并做 RRF 融合；单查询时直接返回该查询的排序结果
    async def _retrieve(
        self,
        queries: list[str],
        limit: int,
        allowed: set[int] | None,
        cond: dict[str, object] | None,
    ) -> list[tuple[int, float]]:
        if len(queries) == 1:
            return await self._index.search(queries[0], limit, allowed, cond)
        depth = max(limit, self._query_depth)
        ranked: list[list[tuple[int, float]]] = []
        for q in queries:
            ranked.append(await self._index.search(q, depth, allowed, cond))
        return rrf_fuse(ranked)[:limit]

    # 返回知识库主题清单的纯文本，供注入 System Prompt 或工具描述
    def manifest(self) -> str:
        if not self._topics:
            return ""
        return "知识库当前覆盖主题：" + "、".join(self._topics)

    # 返回可过滤字段及其全部取值（词汇表），供 LLM 生成合法过滤条件；值经 canonical 统一展示
    def metadata_manifest(self) -> str:
        fields: dict[str, set[str]] = {}
        for c in self._chunks:
            for k, v in c.metadata.items():
                fields.setdefault(k, set()).add(canonical(v))
        if not fields:
            return ""
        lines = ["可过滤字段及其取值："]
        for k, vals in sorted(fields.items()):
            lines.append(f"- {k}: " + "、".join(sorted(vals)))
        return "\n".join(lines)

    # 把过滤条件翻译为命中 chunk 的下标集合；任一字段不匹配则剔除
    def _match(self, cond: dict[str, object]) -> set[int]:
        if not cond:
            return set(range(len(self._chunks)))
        # 先用倒排索引求各字段命中下标的交集，避免全量线性扫描
        groups = [self._match_field(k, v) for k, v in cond.items()]
        if not groups:
            return set()
        result = groups[0]
        for g in groups[1:]:
            result = result & g
        return result

    # 返回单个字段条件命中的 chunk 下标集合；倒排索引缺失字段时退回线性扫描兜底
    def _match_field(self, key: str, cond: object) -> set[int]:
        inv = self._inverted.get(key)
        if inv is not None:
            out: set[int] = set()
            for val, idxs in inv.items():
                if matches(val, cond):
                    out |= idxs
            return out
        out = set()
        for i, c in enumerate(self._chunks):
            actual = c.metadata.get(key)
            if actual is not None and matches(actual, cond):
                out.add(i)
        return out

    # 构建字段值倒排索引：键为 canonical 后的取值，值为命中 chunk 下标集合
    def _build_inverted(self, chunks: list[RagChunk]) -> dict[str, dict[str, set[int]]]:
        inverted: dict[str, dict[str, set[int]]] = {}
        for i, c in enumerate(chunks):
            for k, v in c.metadata.items():
                bucket = inverted.setdefault(k, {})
                bucket.setdefault(canonical(v), set()).add(i)
        return inverted

    # 返回文件 mtime 的 YYYY-MM 字符串，作 updated_at 元数据；IO 错误返回空
    def _updated_at(self, f: Path) -> str:
        try:
            return datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m")
        except OSError:
            return ""


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
