from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from kama_claude.core.compact.token_budget import estimate_tokens
from kama_claude.core.config import MemoryConfig
from kama_claude.core.journal import current_journal
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
        delta = (b - a).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return 0.0
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
        self._vector_dirty = True
        self._vector_entries: list[MemoryEntry] = []
        self._lock = asyncio.Lock()
        self._consolidation_lock = asyncio.Lock()

    # 关闭 sqlite 连接与向量层索引，释放持久层与网络资源
    async def aclose(self) -> None:
        self._sqlite.close()
        if self._vector is not None:
            await self._vector.aclose()

    # 启动时恢复：从 sqlite 读全部条目，重建向量层的派生索引（含 vector 路由子集）
    async def load(self) -> None:
        entries = self._sqlite.all_entries()
        # 只重建 active 条目，退休（superseded/expired）记忆不再嵌入向量索引
        self._vector_entries = [
            e for e in entries if self._usable(e) and self._routes_to_vector(e)
        ]
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
        source: str = "agent_note",
        expires_at: str | None = None,
    ) -> MemoryEntry | None:
        content = content.strip()
        if not content:
            return None
        journal = current_journal.get()
        entry = MemoryEntry(
            id=new_memory_id(content, project),
            content=content,
            kind=kind,
            project=project,
            importance=max(0.0, min(1.0, importance)),
            tags=list(tags or []),
            source=source,
            source_run_id=journal.data["run_id"] if journal is not None else "",
            expires_at=expires_at,
        )
        # 加锁串行化向量索引的追加与重建，避免与整合的 load() 重赋值交错
        async with self._lock:
            # 结构化层去重：id 相同（内容 hash 一致）说明已存在，直接跳过
            previous = self._sqlite.get(entry.id)
            legacy = self._sqlite.get(new_memory_id(content))
            if previous is not None and self._usable(previous):
                return None
            if legacy is not None and legacy.project == project and self._usable(legacy):
                return None
            # 向量层去重：语义相似度超阈值的既有条目视为重复，跳过写入
            if self._routes_to_vector(entry) and await self._is_duplicate(entry):
                return None
            self._sqlite.upsert(entry)
            if self._routes_to_vector(entry):
                self._vector_entries = [
                    item for item in self._vector_entries if item.id != entry.id
                ]
                self._vector_entries.append(entry)
                await self._rebuild_vector()
            return entry

    # 判断新条目是否与向量层既有条目语义重复
    async def _is_duplicate(self, entry: MemoryEntry) -> bool:
        if self._vector is None:
            return False
        hits = await self._vector.search(
            entry.content, top_k=1, filter={"kind": entry.kind, "project": entry.project},
        )
        if not hits:
            return False
        best_entry, sim = hits[0]
        current = self._sqlite.get(best_entry.id)
        return (
            current is not None and self._usable(current)
            and current.project == entry.project and best_entry.id != entry.id
            and sim >= self._config.dedup_threshold
        )

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
        if self._vector_dirty:
            async with self._lock:
                await self.load()
            if self._vector_dirty:
                return []
        hits = await self._vector.search(query, top_k, filter=filter)
        project_filter = (filter or {}).get("project")
        allowed_projects: set[str] | None = None
        if isinstance(project_filter, str):
            allowed_projects = {project_filter}
        elif isinstance(project_filter, list):
            allowed_projects = {str(project) for project in project_filter}
        elif project_filter is not None:
            raise ValueError("invalid project filter")
        hits = [
            (current, score)
            for entry, score in hits
            if (current := self._sqlite.get(entry.id)) is not None and self._usable(current)
            and (allowed_projects is None or current.project in allowed_projects)
        ]
        now = _now()
        rescored = sorted(hits, key=lambda h: self._score(h[1], h[0], now), reverse=True)
        for entry, _ in rescored:
            entry.last_accessed_at = now
        # 批量刷新访问时间，单事务提交避免逐条 commit 写放大
        self._sqlite.touch_many([e.id for e, _ in rescored], now)
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
        entries = [
            entry for entry in self._sqlite.query(kind=kind, project=project, tags=tags)
            if self._usable(entry)
        ]
        if limit is not None:
            entries = entries[:max(0, limit)]
        now = _now()
        for entry in entries:
            entry.last_accessed_at = now
        # 批量刷新访问时间，单事务提交避免逐条 commit 写放大
        self._sqlite.touch_many([e.id for e in entries], now)
        return entries

    # 从权威存储判断时效，损坏的有效期不会静默作为新鲜事实注入
    def _usable(self, entry: MemoryEntry) -> bool:
        if entry.status != "active":
            return False
        if entry.expires_at is None:
            return True
        try:
            expiry = datetime.fromisoformat(entry.expires_at)
            return expiry.tzinfo is not None and expiry > datetime.now(UTC)
        except (TypeError, ValueError):
            return False

    # 衰减评分：语义相似度 × 重要度提升 × 新鲜度（半衰期衰减）
    def _score(self, similarity: float, entry: MemoryEntry, now: str) -> float:
        age_days = days_between(entry.last_accessed_at, now)
        recency = 1.0 / (1.0 + age_days / self._config.half_life_days)
        return similarity * (0.5 + 0.5 * entry.importance) * recency

    # 主动注入块：高重要度偏好 + 近期知识/决策，拼成注入 System Prompt 的纯文本
    async def inject(
        self, top_n: int | None = None, project: str = "", token_budget: int | None = None,
    ) -> str:
        top_n = top_n or self._config.inject_top_n
        # 偏好按重要度降序挑选（而非更新时间），兑现"高重要度偏好"的注入承诺
        prefs: dict[str, MemoryEntry] = {}
        for e in self._sqlite.query(kind="preference", project=project):
            prefs[e.id] = e
        if project:
            for e in self._sqlite.query(kind="preference", project=""):
                prefs[e.id] = e
        ranked_prefs = sorted(
            (entry for entry in prefs.values() if self._usable(entry)),
            key=lambda entry: entry.importance, reverse=True,
        )[:top_n]
        # 非偏好条目按最近访问时间降序，代表"近期知识/决策"
        recent = sorted(
            (
                e
                for e in self._sqlite.query(status="active")
                if e.kind != "preference" and (e.project == project or e.project == "")
                and self._usable(e)
            ),
            key=lambda e: e.last_accessed_at,
            reverse=True,
        )[:top_n]
        chosen: dict[str, MemoryEntry] = {}
        for e in ranked_prefs + recent:
            chosen.setdefault(e.id, e)
        if not chosen:
            return ""
        lines: list[str] = []
        for entry in chosen.values():
            if not self._usable(entry):
                continue
            provenance = (
                f" (id={entry.id}, source={entry.source or 'legacy'}, "
                f"verified={entry.verified_at or 'unverified'})"
            ) if token_budget is not None else ""
            line = f"- [{entry.kind}] {entry.content}{provenance}"
            candidate = "\n".join(lines + [line])
            if token_budget is not None and estimate_tokens(candidate) > token_budget:
                continue
            lines.append(line)
            if len(lines) >= top_n:
                break
        return "\n".join(lines)

    # 显式撤销当前项目或全局记忆，后续注入与搜索立即排除
    async def retire(self, memory_id: str, project: str) -> None:
        async with self._lock:
            entry = self._sqlite.get(memory_id)
            if entry is None or entry.project not in {"", project}:
                raise ValueError("memory not found in authorized project scope")
            self._sqlite.set_status(memory_id, "expired", _now())
            self._vector_dirty = True

    # 返回 active 记忆总数，供阈值判断
    def count(self) -> int:
        return self._sqlite.count()

    # 返回是否超过 max_entries 阈值，触发自动整合
    def needs_consolidation(self) -> bool:
        return self.count() >= self._config.max_entries

    # 触发整合：LLM 审查在锁外执行，写回与索引重建在锁内，避免长时间阻塞并发 add
    async def consolidate(self, provider: LLMProvider) -> ConsolidationResult | None:
        if self._consolidation_lock.locked():
            return None
        async with self._consolidation_lock:
            return await self._consolidate(provider)

    # 使用版本校验与 SQLite 原子事务提交记忆整合结果
    async def _consolidate(self, provider: LLMProvider) -> ConsolidationResult | None:
        from kama_claude.core.memory.consolidator import Consolidator

        consolidator = Consolidator(self._config)
        revision = self._sqlite.revision
        result = await consolidator.consolidate(
            provider, [entry for entry in self._sqlite.all_entries() if self._usable(entry)],
        )
        if result is None:
            return None
        async with self._lock:
            if self._sqlite.revision != revision:
                log.info("memory consolidation skipped: snapshot changed")
                return None
            with self._sqlite.transaction():
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
        try:
            await self._vector.build(self._vector_entries)
            self._vector_dirty = False
        except Exception:
            self._vector_dirty = True
            log.warning(
                "memory vector rebuild pending; SQLite remains authoritative", exc_info=True
            )
