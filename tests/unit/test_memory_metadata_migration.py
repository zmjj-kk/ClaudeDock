from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from claudedock.core.memory.sqlite_store import MemorySqliteStore


# 构造升级前的真实数据库，避免新建表路径掩盖迁移问题
def legacy_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT NOT NULL, "
            "kind TEXT NOT NULL, project TEXT NOT NULL DEFAULT '', "
            "importance REAL NOT NULL DEFAULT 0.5, tags TEXT NOT NULL DEFAULT '[]', "
            "status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, last_accessed_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("old", "legacy content", "fact", "p", 0.5, "[]", "active", "2026", "2026", "2026"),
        )
        connection.commit()


# 功能：验证旧库原文保留、一致备份有效且迁移可重复打开
# 设计：检查真实 SQLite 备份完整性与新增列，而非只检查文件存在
def test_metadata_migration_has_valid_backup(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    legacy_database(path)
    store = MemorySqliteStore(path)
    try:
        entry = store.get("old")
        assert entry is not None and entry.content == "legacy content"
        assert entry.source == "" and entry.expires_at is None
    finally:
        store.close()
    backup = path.with_name("memory.db.schema-v1.bak")
    with closing(sqlite3.connect(backup)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert "metadata" not in {
            row[1] for row in connection.execute("PRAGMA table_info(memories)")
        }
    reopened = MemorySqliteStore(path)
    reopened.close()


# 功能：验证已有损坏备份时停止迁移，不改变原数据库格式
# 设计：把备份写成非法内容，检查异常后数据库仍可打开且没有新增列
def test_corrupt_backup_blocks_migration(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    legacy_database(path)
    path.with_name("memory.db.schema-v1.bak").write_text("corrupt")
    with pytest.raises(sqlite3.DatabaseError):
        MemorySqliteStore(path)
    with closing(sqlite3.connect(path)) as connection:
        assert "metadata" not in {
            row[1] for row in connection.execute("PRAGMA table_info(memories)")
        }
