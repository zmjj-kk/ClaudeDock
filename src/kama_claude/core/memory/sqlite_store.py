from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from kama_claude.core.memory.model import MemoryEntry, MemoryKind, MemoryStatus
from kama_claude.core.rag.filter import canonical

# sqlite 建表语句：tags 存 JSON 数组字符串，status/kind/project 建立索引加速精确查询
_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    kind TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    importance REAL NOT NULL DEFAULT 0.5,
    tags TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_accessed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mem_kind ON memories(kind);
CREATE INDEX IF NOT EXISTS idx_mem_project ON memories(project);
CREATE INDEX IF NOT EXISTS idx_mem_status ON memories(status);
"""


class MemorySqliteStore:
    # 打开（或创建）sqlite 库并建表；db_path 父目录自动创建
    def __init__(self, db_path: Path) -> None:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # 插入或整体替换一条记忆（按 id 主键 upsert）
    def upsert(self, entry: MemoryEntry) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO memories "
            "(id, content, kind, project, importance, tags, status, "
            "created_at, updated_at, last_accessed_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                entry.id,
                entry.content,
                entry.kind,
                entry.project,
                entry.importance,
                json.dumps(entry.tags, ensure_ascii=False),
                entry.status,
                entry.created_at,
                entry.updated_at,
                entry.last_accessed_at,
            ),
        )
        self._conn.commit()

    # 按 id 读取单条记忆，不存在返回 None
    def get(self, mem_id: str) -> MemoryEntry | None:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE id = ?", (mem_id,)
        ).fetchone()
        return _row_to_entry(row) if row is not None else None

    # 精确查询：kind/project/status 走 SQL 下推，tags 在 Python 侧按 canonical 比对
    def query(
        self,
        *,
        kind: MemoryKind | None = None,
        project: str | None = None,
        tags: list[str] | None = None,
        status: MemoryStatus = "active",
        limit: int | None = None,
    ) -> list[MemoryEntry]:
        clauses: list[str] = ["status = ?"]
        params: list[Any] = [status]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if project is not None:
            clauses.append("project = ?")
            params.append(project)
        sql = f"SELECT * FROM memories WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        entries = [_row_to_entry(r) for r in rows if r is not None]
        if tags:
            wanted = {canonical(t) for t in tags}
            entries = [e for e in entries if wanted & {canonical(t) for t in e.tags}]
        return entries

    # 返回全部记忆（含过期/被取代状态），供整合器全量审查
    def all_entries(self) -> list[MemoryEntry]:
        rows = self._conn.execute("SELECT * FROM memories ORDER BY updated_at DESC").fetchall()
        return [_row_to_entry(r) for r in rows if r is not None]

    # 返回 status=active 的记忆总数，供阈值判断
    def count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM memories WHERE status = 'active'"
        ).fetchone()
        return int(row["n"]) if row is not None else 0

    # 更新单条记忆状态（superseded/expired），并刷新 updated_at
    def set_status(self, mem_id: str, status: MemoryStatus, updated_at: str) -> None:
        self._conn.execute(
            "UPDATE memories SET status = ?, updated_at = ? WHERE id = ?",
            (status, updated_at, mem_id),
        )
        self._conn.commit()

    # 刷新命中记忆的 last_accessed_at（供 recency 衰减评分）
    def touch(self, mem_id: str, accessed_at: str) -> None:
        self._conn.execute(
            "UPDATE memories SET last_accessed_at = ? WHERE id = ?", (accessed_at, mem_id)
        )
        self._conn.commit()

    # 删除单条记忆（整合合并旧条后清理残留）
    def delete(self, mem_id: str) -> None:
        self._conn.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
        self._conn.commit()

    # 关闭底层连接
    def close(self) -> None:
        self._conn.close()


# 把 sqlite Row 还原为 MemoryEntry；tags JSON 解析失败时退回空列表
def _row_to_entry(row: sqlite3.Row) -> MemoryEntry:
    tags_raw = row["tags"]
    try:
        tags = json.loads(tags_raw) if isinstance(tags_raw, str) else []
        if not isinstance(tags, list):
            tags = []
    except ValueError:
        tags = []
    return MemoryEntry(
        id=row["id"],
        content=row["content"],
        kind=row["kind"],
        project=row["project"],
        importance=float(row["importance"]),
        tags=[str(t) for t in tags],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_accessed_at=row["last_accessed_at"],
    )
