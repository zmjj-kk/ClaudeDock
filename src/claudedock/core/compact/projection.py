from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from claudedock.core.compact.microcompact import validate_messages
from claudedock.core.storage import atomic_write


class ContextProjection(BaseModel):
    schema_version: int = 1
    revision: int = Field(ge=1)
    archive_count: int = Field(ge=0)
    messages: list[dict[str, Any]]
    digest: str


# 对投影消息使用稳定序列化摘要，防止恢复半提交或损坏内容
def message_digest(messages: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


class ProjectionStore:
    # 在会话目录维护不可变版本与原子版本指针
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        if directory.is_symlink():
            raise ValueError("projection directory cannot be a symlink")

    # 从版本指针读取已提交消息，损坏时回退上个完整版本
    def read(self) -> ContextProjection | None:
        pointer = self.directory / "context_projection.json"
        if (self.directory / "compact").is_symlink():
            raise ValueError("compact directory cannot be a symlink")
        if not pointer.exists():
            return None
        if pointer.is_symlink():
            raise ValueError("projection pointer cannot be a symlink")
        manifest = json.loads(pointer.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != 1:
            raise ValueError("unsupported projection schema")
        for revision in [manifest["revision"], manifest.get("previous_revision")]:
            if type(revision) is not int or revision < 1:
                continue
            path = self.directory / "compact" / f"projection_{revision}.json"
            try:
                if path.is_symlink():
                    raise ValueError("projection cannot be a symlink")
                projection = ContextProjection.model_validate_json(path.read_text(encoding="utf-8"))
                if (
                    projection.schema_version != 1 or projection.revision != revision
                    or projection.digest != message_digest(projection.messages)
                ):
                    raise ValueError("projection checksum mismatch")
                validate_messages(projection.messages)
                return projection
            except (OSError, ValueError):
                continue
        raise ValueError("no valid context projection")

    # 在会话锁内校验 CAS 并先落版本文件再切换唯一提交指针
    def commit(
        self, messages: list[dict[str, Any]], archive_count: int,
        expected_revision: int | None = None,
    ) -> ContextProjection:
        validate_messages(messages)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        compact = self.directory / "compact"
        if compact.is_symlink():
            raise ValueError("compact directory cannot be a symlink")
        compact.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(
            self.directory / ".projection.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600,
        )
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            old = self.read()
            previous = old.revision if old else 0
            if expected_revision is not None and expected_revision != previous:
                raise ValueError("projection revision conflict")
            pointer = self.directory / "context_projection.json"
            current_revision = (
                json.loads(pointer.read_text(encoding="utf-8"))["revision"]
                if pointer.exists() else 0
            )
            revision = max(previous, current_revision) + 1
            projection = ContextProjection(
                revision=revision, archive_count=archive_count, messages=messages,
                digest=message_digest(messages),
            )
            atomic_write(compact / f"projection_{revision}.json", projection.model_dump_json())
            atomic_write(pointer, json.dumps({
                "schema_version": 1, "revision": revision, "previous_revision": previous,
            }))
            return projection
