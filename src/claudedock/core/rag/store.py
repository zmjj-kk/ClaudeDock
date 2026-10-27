from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from claudedock.core.monitoring.telemetry import observe
from claudedock.core.rag.chunk import (
    extract_topics,
    parse_frontmatter,
    split_with_context,
    strip_frontmatter,
)
from claudedock.core.rag.filter import MetaValue, canonical_values, matches
from claudedock.core.rag.index import Bm25Index, RagIndex, hash_text, rrf_fuse
from claudedock.core.rag.query import QueryProcessor, optimize_query
from claudedock.core.rag.rerank import Reranker
from claudedock.core.storage import atomic_write

# 语义切片算法版本；改动切片逻辑时递增，使旧清单/缓存失效
_CHUNK_VERSION = "4"
# 增量清单结构版本；改动清单字段时递增，使旧清单失效并全量重建
_MANIFEST_VERSION = 2
# 自动注入字段在元数据词汇表中的最大枚举取值数；超出只给字段名与计数，避免撑爆工具描述
_ENUM_FIELD_LIMIT = 20
# 元数据词汇表最多展示的字段数；超出部分只计数，控制每轮注入的 token
_ENUM_MAX_FIELDS = 30

log = logging.getLogger(__name__)


@dataclass
class RagChunk:
    id: str  # 稳定 id："{doc_id}::{序号}"
    source: str  # 源文件绝对路径
    doc_id: str  # 关联文档标识（相对知识库根目录路径），供按文档聚合/删除
    text: str
    content_hash: str  # chunk 文本的 sha256，供向量缓存按内容复用
    topic: str = ""  # 所属主题标签（文档级），仅用于 manifest 清单展示，不作过滤条件
    heading: str = ""  # 所属章节标题（块级），用于结果展示与判断片段归属
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
        self._refresh_lock = asyncio.Lock()
        self._readers: dict[int, int] = {}
        self._retired: list[RagIndex] = []
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
        self._docs: dict[str, dict[str, Any]] = {}  # 上次加载的文档清单，供 reload 增量比对
        self._docs_signature = ""  # 清单内容指纹，未变时 reload 直接跳过重建

    # 关闭检索后端与 reranker，释放底层网络资源
    async def aclose(self) -> None:
        await self._index.aclose()
        if self._reranker is not None:
            await self._reranker.aclose()

    # 递归加载目录下的文本文件，按内容 hash 增量重建索引，返回 chunk 数
    async def load_dir(self, directory: Path, exts: tuple[str, ...] = (".md", ".txt")) -> int:
        directory = Path(directory)
        if not directory.is_dir():
            return 0
        self._manifest_path = directory / ".rag_index_manifest.json"
        prev_docs = self._load_manifest()
        docs = self._load_docs(directory, prev_docs, exts)
        return await self._rebuild(docs)

    # 重扫知识库目录，仅重解析内容或元数据变化的文档，未变文档复用已缓存 chunk
    async def reload(self) -> int:
        if self._manifest_path is None:
            return len(self._chunks)
        directory = self._manifest_path.parent
        prev_docs = self._load_manifest()
        docs = self._load_docs(directory, prev_docs, (".md", ".txt"))
        # 内容与元数据均未变时跳过重建，避免 daemon 每轮 run 重复建索引
        if _docs_signature(docs) == self._docs_signature:
            return len(self._chunks)
        return await self._rebuild(docs)

    # 解析目录下全部文档为清单记录：未变文档直接复用旧 chunk，返回 doc_id → 记录
    def _load_docs(
        self, directory: Path, prev_docs: dict[str, dict[str, Any]], exts: tuple[str, ...]
    ) -> dict[str, dict[str, Any]]:
        files: list[Path] = []
        for ext in exts:
            files.extend(directory.rglob(f"*{ext}"))
        current: dict[str, Path] = {
            f.relative_to(directory).as_posix(): f for f in sorted(set(files))
        }
        docs: dict[str, dict[str, Any]] = {}
        for doc_id in sorted(current):
            f = current[doc_id]
            raw = self._read(f)
            if raw is None:
                continue  # 读取失败：丢弃该文档，下次再试
            content_hash = hash_text(raw)
            prev = prev_docs.get(doc_id)
            if prev is not None and content_hash == prev.get("content_hash"):
                # 内容未变：复用已缓存的 chunk 与标题，元信息直接沿用旧记录
                docs[doc_id] = dict(prev)
                continue
            docs[doc_id] = self._parse_doc(doc_id, f, raw, content_hash)
        return docs

    # 把单个文档解析为清单记录：切片（含标题上下文）、主题与结构化元数据
    def _parse_doc(self, doc_id: str, f: Path, raw: str, content_hash: str) -> dict[str, Any]:
        # 先切掉 frontmatter 再切片：元数据块不应进入检索正文与主题清单
        body = strip_frontmatter(raw)
        pairs = split_with_context(body, self._chunk_size, self._chunk_overlap)
        # 提取文档级主题：取首个标题作为归属主题，其余标题并入全局清单
        doc_topics = extract_topics(body)
        # 结构化元数据：来源路径、更新时间、frontmatter 显式字段
        meta: dict[str, MetaValue] = {"source": doc_id, "updated_at": self._updated_at(f)}
        meta.update(parse_frontmatter(raw))
        return {
            "path": str(f),
            "content_hash": content_hash,
            "chunks": [text for text, _ in pairs],
            "headings": [heading for _, heading in pairs],
            "topics": doc_topics,
            "topic": doc_topics[0] if doc_topics else "",
            "metadata": meta,
        }

    # 由清单记录组装 chunk 列表并重建索引，返回 chunk 数
    async def _rebuild(self, docs: dict[str, dict[str, Any]]) -> int:
        async with self._refresh_lock:
            return await self._build_snapshot(docs)

    # 完整构建新索引后同步发布所有元数据，失败时保留上一版
    async def _build_snapshot(self, docs: dict[str, dict[str, Any]]) -> int:
        chunks: list[RagChunk] = []
        topics: list[str] = []
        for doc_id in sorted(docs):
            rec = docs[doc_id]
            texts = rec.get("chunks") or []
            headings = rec.get("headings") or [""] * len(texts)
            meta = dict(rec.get("metadata", {}))
            topic = rec.get("topic", "")
            for i, text in enumerate(texts):
                chunks.append(
                    RagChunk(
                        id=f"{doc_id}::{i}",
                        source=rec.get("path", ""),
                        doc_id=doc_id,
                        text=text,
                        content_hash=hash_text(text),
                        topic=topic,
                        heading=headings[i] if i < len(headings) else "",
                        metadata=meta,
                    )
                )
            topics.extend(rec.get("topics", []))
        index = self._index.fork()
        metas = [dict(c.metadata) for c in chunks]
        try:
            await index.build(
                [c.text for c in chunks], [c.id for c in chunks], self._build_token(), metas
            )
        except BaseException:
            await self._retire(index)
            raise
        inverted = self._build_inverted(chunks)
        previous = self._index
        self._index = index
        self._docs = docs
        self._docs_signature = _docs_signature(docs)
        self._chunks = chunks
        self._topics = _dedupe(topics)
        self._inverted = inverted
        self._save_manifest(docs)
        if self._readers.get(id(previous), 0):
            self._retired.append(previous)
        else:
            await self._retire(previous)
        return len(chunks)

    # 记录检索端到端真实耗时，查询正文保留在权限边界内
    async def search(
        self, query: str, top_k: int = 3, filter: dict[str, object] | None = None
    ) -> list[RagChunk]:
        with observe("rag.search", {}):
            return await self._leased_search(query, top_k, filter)

    # 检索与 query 最相似的 top_k 个 chunk；filter 非空时按元数据过滤候选集
    async def _leased_search(
        self,
        query: str,
        top_k: int = 3,
        filter: dict[str, object] | None = None,
    ) -> list[RagChunk]:
        snapshot = copy.copy(self)
        index = snapshot._index
        key = id(index)
        self._readers[key] = self._readers.get(key, 0) + 1
        try:
            return await snapshot._search_snapshot(query, top_k, filter)
        finally:
            self._readers[key] -= 1
            if self._readers[key] == 0:
                self._readers.pop(key)
                if index in self._retired:
                    self._retired.remove(index)
                    await self._retire(index)

    # 回收失败只影响派生快照，不破坏已发布的检索版本
    async def _retire(self, index: RagIndex) -> None:
        try:
            await index.retire()
        except Exception:
            log.warning("RAG snapshot cleanup failed", exc_info=True)

    # 一次查询固定使用同一版索引、chunk 和过滤元数据
    async def _search_snapshot(
        self,
        query: str,
        top_k: int,
        filter: dict[str, object] | None,
    ) -> list[RagChunk]:
        if not self._chunks or not query.strip():
            return []
        # 过滤：翻译成命中 chunk 下标白名单，供内存后端二次筛选；非 dict 条件安全忽略
        cond = dict(filter) if isinstance(filter, dict) else {}
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

    # 返回可过滤字段及其取值（词汇表），供 LLM 生成合法过滤条件；
    # 基数过高的字段只给字段名与计数，避免把成百上千条路径注入每轮工具描述
    def metadata_manifest(self) -> str:
        fields: dict[str, set[str]] = {}
        for c in self._chunks:
            for k, v in c.metadata.items():
                fields.setdefault(k, set()).update(canonical_values(v))
        if not fields:
            return ""
        lines = ["可过滤字段及其取值："]
        omitted = 0
        for k, vals in sorted(fields.items()):
            if len(vals) > _ENUM_FIELD_LIMIT:
                lines.append(f"- {k}: 共 {len(vals)} 个取值（精确匹配该字段）")
                continue
            if len(lines) > _ENUM_MAX_FIELDS:
                omitted += 1
                continue
            lines.append(f"- {k}: " + "、".join(sorted(vals)))
        if omitted:
            lines.append(f"（另有 {omitted} 个字段未列出，同样可作精确匹配）")
        return "\n".join(lines)

    # 把过滤条件翻译为命中 chunk 的下标集合；先用倒排索引求各字段交集，避免全量线性扫描
    def _match(self, cond: dict[str, object]) -> set[int]:
        if not cond:
            return set(range(len(self._chunks)))
        metadatas = [c.metadata for c in self._chunks]
        out: set[int] | None = None
        for key, val in cond.items():
            hit = self._match_field(key, val, metadatas)
            out = hit if out is None else out & hit
            if not out:
                return set()
        return out if out is not None else set()

    # 返回单个字段条件命中的 chunk 下标集合；倒排索引缺失该字段时退回线性扫描兜底
    def _match_field(
        self, key: str, cond: object, metadatas: list[dict[str, MetaValue]]
    ) -> set[int]:
        inv = self._inverted.get(key)
        if inv is not None:
            out: set[int] = set()
            for val, idxs in inv.items():
                if matches(val, cond):
                    out |= idxs
            return out
        return {i for i, m in enumerate(metadatas) if key in m and matches(m[key], cond)}

    # 构建字段值倒排索引：键为 canonical 后的取值，值为命中 chunk 下标集合；
    # 列表值逐元素入桶，使按标签过滤能直接命中，无需退回线性扫描
    def _build_inverted(self, chunks: list[RagChunk]) -> dict[str, dict[str, set[int]]]:
        inverted: dict[str, dict[str, set[int]]] = {}
        for i, c in enumerate(chunks):
            for k, v in c.metadata.items():
                bucket = inverted.setdefault(k, {})
                for cv in canonical_values(v):
                    bucket.setdefault(cv, set()).add(i)
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
            atomic_write(self._manifest_path, json.dumps(payload, ensure_ascii=False))
        except OSError:
            log.warning("failed to write RAG manifest: %s", self._manifest_path)


# 计算文档清单的内容指纹：仅由文档内容 hash 与其元数据派生，用于跳过无变化的重建
def _docs_signature(docs: dict[str, dict[str, Any]]) -> str:
    h = hashlib.sha256()
    for doc_id in sorted(docs):
        rec = docs[doc_id]
        h.update(doc_id.encode("utf-8"))
        h.update(str(rec.get("content_hash", "")).encode("utf-8"))
        h.update(
            json.dumps(rec.get("metadata", {}), sort_keys=True, ensure_ascii=False).encode("utf-8")
        )
    return h.hexdigest()


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
