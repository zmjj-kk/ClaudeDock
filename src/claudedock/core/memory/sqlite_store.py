from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from claudedock.core.memory.model import MemoryEntry, MemoryKind, MemoryStatus
from claudedock.core.rag.filter import canonical

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
    last_accessed_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_mem_kind ON memories(kind);
CREATE INDEX IF NOT EXISTS idx_mem_project ON memories(project);
CREATE INDEX IF NOT EXISTS idx_mem_status ON memories(status);
"""


_METADATA_FIELDS = (
    "source", "source_run_id", "verified_at", "expires_at",
    "confidence", "policy_tags", "schema_version",
)


class MemorySqliteStore:
    # 打开（或创建）sqlite 库并建表；db_path 父目录自动创建
    def __init__(self, db_path: Path) -> None:
        db_path = Path(db_path)
        if db_path.is_symlink():
            raise ValueError("memory database cannot be a symlink")
        db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        db_path.parent.chmod(0o700)
        self._conn = sqlite3.connect(str(db_path))
        db_path.chmod(0o600)
        self._revision = 0
        self._in_transaction = False
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._commit()
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(memories)")}
        if "metadata" not in columns:
            try:
                backup = db_path.with_name(db_path.name + ".schema-v1.bak")
                if backup.is_symlink():
                    raise ValueError("memory backup cannot be a symlink")
                self._backup_before_migration(backup)
                with self.transaction():
                    current = {
                        row["name"] for row in self._conn.execute("PRAGMA table_info(memories)")
                    }
                    if "metadata" not in current:
                        self._conn.execute(
                            "ALTER TABLE memories ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'"
                        )
            except BaseException:
                self._conn.close()
                raise

    # 使用 SQLite 一致快照与原子替换生成迁移备份，失败备份不能伪装成可恢复版本
    def _backup_before_migration(self, backup: Path) -> None:
        if backup.exists():
            with closing(sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True)) as target:
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("existing migration backup is corrupt")
            return
        fd, temporary_name = tempfile.mkstemp(prefix=".memory-backup-", dir=backup.parent)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            with closing(sqlite3.connect(temporary)) as target:
                self._conn.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("migration backup failed integrity validation")
            with temporary.open("r+b") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, backup)
            if hasattr(os, "O_DIRECTORY"):
                directory = os.open(backup.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if temporary.exists():
                temporary.unlink()

    # 获取持久层修改版本，供锁外整合结果检查快照是否过期
    @property
    def revision(self) -> tuple[int, int]:
        external = int(self._conn.execute("PRAGMA data_version").fetchone()[0])
        return self._revision, external

    # 非事务批次立即提交，事务内由统一出口提交
    def _commit(self) -> None:
        if not self._in_transaction:
            self._conn.commit()
            self._revision += 1

    # 整合批次原子提交，任一步失败都回滚全部更改
    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._in_transaction:
            raise RuntimeError("nested memory transaction")
        self._conn.execute("BEGIN IMMEDIATE")
        self._in_transaction = True
        try:
            yield
            self._conn.commit()
            self._revision += 1
        except BaseException:
            self._conn.rollback()
            raise
        finally:
            self._in_transaction = False

    # 插入或整体替换一条记忆（按 id 主键 upsert）
    def upsert(self, entry: MemoryEntry) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO memories "
            "(id, content, kind, project, importance, tags, status, "
            "created_at, updated_at, last_accessed_at, metadata) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
                json.dumps({key: entry.to_dict()[key] for key in _METADATA_FIELDS}),
            ),
        )
        self._commit()

    # 按 id 读取单条记忆，不存在返回 None
    def get(self, mem_id: str) -> MemoryEntry | None:
        row = self._conn.execute("SELECT * FROM memories WHERE id = ?", (mem_id,)).fetchone()
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
        rows = self._conn.execute(
            "SELECT * FROM memories WHERE status = ? "
            "AND (? IS NULL OR kind = ?) AND (? IS NULL OR project = ?) "
            "ORDER BY updated_at DESC LIMIT ?",
            (status, kind, kind, project, project, limit if limit is not None else -1),
        ).fetchall()
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
        self._commit()

    # 刷新命中记忆的 last_accessed_at（供 recency 衰减评分）
    def touch(self, mem_id: str, accessed_at: str) -> None:
        self._conn.execute(
            "UPDATE memories SET last_accessed_at = ? WHERE id = ?", (accessed_at, mem_id)
        )
        self._commit()

    # 批量刷新多条记忆的 last_accessed_at，单事务提交，避免逐条 commit 的写放大
    def touch_many(self, mem_ids: list[str], accessed_at: str) -> None:
        if not mem_ids:
            return
        self._conn.executemany(
            "UPDATE memories SET last_accessed_at = ? WHERE id = ?",
            [(accessed_at, mid) for mid in mem_ids],
        )
        self._commit()

    # 删除单条记忆（整合合并旧条后清理残留）
    def delete(self, mem_id: str) -> None:
        self._conn.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
        self._commit()

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
    try:
        metadata = json.loads(row["metadata"])
        if not isinstance(metadata, dict):
            metadata = {}
    except (ValueError, IndexError):
        metadata = {}
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
        **{key: metadata[key] for key in _METADATA_FIELDS if key in metadata},
    )
