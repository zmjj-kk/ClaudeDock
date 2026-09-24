from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from kama_claude.core.config import MemoryConfig
from kama_claude.core.memory.model import MemoryEntry, MemoryKind, new_memory_id
from kama_claude.core.memory.sqlite_store import MemorySqliteStore
from kama_claude.core.memory.vector_store import MemoryVectorStore

if TYPE_CHECKING:
    from kama_claude.core.llm.base import LLMProvider
    from kama_claude.core.memory.consolidator import ConsolidationResult

log = logging.getLogger(__name__)


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 计算两个 ISO 时间戳之间的天数差，解析失败或倒挂时按 0 处理
def days_between(earlier: str, later: str) -> float:
    try:
        a = datetime.fromisoformat(earlier)
        b = datetime.fromisoformat(later)
    except ValueError:
        return 0.0
    delta = (b - a).total_seconds() / 86400.0
    return delta if delta > 0 else 0.0


class MemoryStore:
    # 门面构造：绑定 sqlite（权威持久层）与向量（派生语义索引）；可注入双层以便测试
    def __init__(
        self,
        config: MemoryConfig,
        *,
        sqlite_store: MemorySqliteStore | None = None,
        vector_store: MemoryVectorStore | None = None,
    ) -> None:
        self._config = config
        self._sqlite = sqlite_store or MemorySqliteStore(
            Path(config.store_dir).expanduser() / "memories.db"
        )
        self._vector = vector_store
        self._vector_entries: list[MemoryEntry] = []
        self._lock = asyncio.Lock()

    # 启动时恢复：从 sqlite 读全部条目，重建向量层的派生索引（含 vector 路由子集）
    async def load(self) -> None:
        entries = self._sqlite.all_entries()
        self._vector_entries = [e for e in entries if self._routes_to_vector(e)]
        await self._rebuild_vector()
        log.info("memory loaded: %d entries (%d vector)", len(entries), len(self._vector_entries))

    # 判断条目是否走向量层：knowledge 恒走，decision/fact 长于阈值走，preference 恒走 sqlite
    def _routes_to_vector(self, entry: MemoryEntry) -> bool:
        if entry.kind == "knowledge":
            return True
        if entry.kind in ("decision", "fact"):
            return len(entry.content) > self._config.max_structured_len
        return False

    # 写入一条记忆：内容去重 → sqlite upsert → 必要时重建向量索引；重复时返回 None
    async def add(
        self,
        content: str,
        *,
        kind: MemoryKind,
        project: str = "",
        importance: float = 0.5,
        tags: list[str] | None = None,
    ) -> MemoryEntry | None:
        content = content.strip()
        if not content:
            return None
        entry = MemoryEntry(
            id=new_memory_id(content),
            content=content,
            kind=kind,
            project=project,
            importance=max(0.0, min(1.0, importance)),
            tags=list(tags or []),
        )
        # 结构化层去重：id 相同（内容 hash 一致）说明已存在，直接跳过
        if self._sqlite.get(entry.id) is not None:
            return None
        # 向量层去重：语义相似度超阈值的既有条目视为重复，跳过写入
        if self._routes_to_vector(entry) and await self._is_duplicate(entry):
            return None
        self._sqlite.upsert(entry)
        if self._routes_to_vector(entry):
            self._vector_entries.append(entry)
            await self._rebuild_vector()
        return entry

    # 判断新条目是否与向量层既有条目语义重复
    async def _is_duplicate(self, entry: MemoryEntry) -> bool:
        if self._vector is None:
            return False
        hits = await self._vector.search(entry.content, top_k=1, filter={"kind": entry.kind})
        if not hits:
            return False
        best_entry, sim = hits[0]
        return best_entry.id != entry.id and sim >= self._config.dedup_threshold

    # 语义检索：向量层召回后叠加 importance×recency 重排，命中条目 touch 刷新访问时间
    async def search(
        self,
        query: str,
        top_k: int | None = None,
        filter: dict[str, object] | None = None,
    ) -> list[MemoryEntry]:
        if self._vector is None:
            return []
        top_k = top_k or self._config.search_top_k
        hits = await self._vector.search(query, top_k, filter=filter)
        now = _now()
        rescored = sorted(
            hits, key=lambda h: self._score(h[1], h[0], now), reverse=True
        )
        for entry, _ in rescored:
            self._sqlite.touch(entry.id, now)
            entry.last_accessed_at = now
        return [e for e, _ in rescored]

    # 精确查询：走 sqlite 结构化过滤，命中条目 touch 刷新访问时间
    async def query(
        self,
        *,
        kind: MemoryKind | None = None,
        project: str | None = None,
        tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[MemoryEntry]:
        entries = self._sqlite.query(kind=kind, project=project, tags=tags, limit=limit)
        now = _now()
        for entry in entries:
            self._sqlite.touch(entry.id, now)
            entry.last_accessed_at = now
        return entries

    # 衰减评分：语义相似度 × 重要度提升 × 新鲜度（半衰期衰减）
    def _score(self, similarity: float, entry: MemoryEntry, now: str) -> float:
        age_days = days_between(entry.last_accessed_at, now)
        recency = 1.0 / (1.0 + age_days / self._config.half_life_days)
        return similarity * (0.5 + 0.5 * entry.importance) * recency

    # 主动注入块：高重要度偏好 + 近期知识/决策，拼成注入 System Prompt 的纯文本
    async def inject(self, top_n: int | None = None, project: str = "") -> str:
        top_n = top_n or self._config.inject_top_n
        prefs = self._sqlite.query(kind="preference", project=project, limit=top_n)
        prefs += self._sqlite.query(kind="preference", project="", limit=top_n)
        recent = [
            e for e in self._sqlite.query(status="active", limit=top_n * 4)
            if e.kind != "preference" and (e.project == project or e.project == "")
        ][:top_n]
        chosen: dict[str, MemoryEntry] = {}
        for e in prefs + recent:
            chosen.setdefault(e.id, e)
        if not chosen:
            return ""
        lines = [f"- [{e.kind}] {e.content}" for e in list(chosen.values())[:top_n]]
        return "\n".join(lines)

    # 返回 active 记忆总数，供阈值判断
    def count(self) -> int:
        return self._sqlite.count()

    # 返回是否超过 max_entries 阈值，触发自动整合
    def needs_consolidation(self) -> bool:
        return self.count() >= self._config.max_entries

    # 触发整合：加锁防并发，调 Consolidator 后把结果写回 sqlite 并重建向量索引
    async def consolidate(self, provider: LLMProvider) -> ConsolidationResult | None:
        async with self._lock:
            from kama_claude.core.memory.consolidator import Consolidator

            consolidator = Consolidator(self._config)
            result = await consolidator.consolidate(provider, self._sqlite.all_entries())
            if result is None:
                return None
            for replacement in result.replace:
                self._sqlite.upsert(replacement)
            for mem_id, status in result.retire:
                self._sqlite.set_status(mem_id, status, _now())
            for mem_id in result.delete:
                self._sqlite.delete(mem_id)
            await self.load()
            return result

    # 重建向量层派生索引；未注入 vector 层时跳过
    async def _rebuild_vector(self) -> None:
        if self._vector is None:
            return
        await self._vector.build(self._vector_entries)
