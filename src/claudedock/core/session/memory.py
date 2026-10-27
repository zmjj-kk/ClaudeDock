from __future__ import annotations

import fcntl
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from claudedock.core.security.redaction import redact
from claudedock.core.storage import atomic_write


@dataclass
class SessionNoteState:
    schema_version: int = 1
    revision: int = 0
    goal: str = ""
    active_goal: str = ""
    requests: list[str] = field(default_factory=list)
    completed_verified: list[dict[str, Any]] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    files_touched: dict[str, dict[str, Any]] = field(default_factory=dict)
    known_failures: list[dict[str, str]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    source_event_ids: list[str] = field(default_factory=list)
    source_run_id: str = ""
    updated_at: str = ""

    # 输出受预算约束的辅助笔记，原始请求仍以 transcript 为准
    def render(self, max_bytes: int = 8192) -> str:
        data = redact(asdict(self))
        text = json.dumps(data, ensure_ascii=False)
        while len(text.encode("utf-8")) > max_bytes:
            for key in ("source_event_ids", "completed_verified", "known_failures", "requests"):
                if data[key]:
                    data[key].pop(0)
                    break
            else:
                return "[Session notes exceed injection budget; consult session context view]"
            text = json.dumps(data, ensure_ascii=False)
        return text


class SessionNoteStore:
    # 独立存储结构化辅助笔记，不覆盖用户主动保存的 notes.md
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "session_memory.json"

    # 兼容尚无自动笔记的旧会话，并拒绝损坏或不支持的格式
    def read(self) -> SessionNoteState:
        if not self.path.exists():
            return SessionNoteState()
        if self.path.is_symlink():
            raise ValueError("session memory cannot be a symlink")
        raw = self.path.read_bytes()
        if len(raw) > 262144:
            raise ValueError("session memory exceeds storage limit")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("unsupported session memory schema")
        if type(data.get("revision")) is not int or data["revision"] < 0:
            raise ValueError("invalid session memory revision")
        for key in (
            "requests", "completed_verified", "pending", "known_failures",
            "open_questions", "source_event_ids",
        ):
            if not isinstance(data.get(key), list) or len(data[key]) > 256:
                raise ValueError("invalid session memory collections")
        if not isinstance(data.get("files_touched"), dict):
            raise ValueError("invalid session memory file state")
        try:
            return SessionNoteState(**data)
        except TypeError as exc:
            raise ValueError("invalid session memory fields") from exc

    # 通过会话文件锁与版本 CAS 阻止陈旧后台结果覆盖新笔记
    def compare_and_swap(self, candidate: SessionNoteState, expected_revision: int) -> bool:
        if self.directory.is_symlink():
            raise ValueError("session directory cannot be a symlink")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(
            self.directory / ".session_memory.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600,
        )
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = self.read()
            if current.revision != expected_revision:
                return False
            candidate.revision = expected_revision + 1
            atomic_write(self.path, json.dumps(redact(asdict(candidate)), ensure_ascii=False))
            return True
