from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

# 记忆类别：preference=用户偏好/习惯，decision=关键结论与决策，
# fact=结构化事实，knowledge=高频外部知识
MemoryKind = Literal["preference", "decision", "fact", "knowledge"]
# 记忆状态：active=有效，superseded=被更新版本取代，expired=因衰减/矛盾而失效
MemoryStatus = Literal["active", "superseded", "expired"]


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 由正文内容派生稳定 id（mem_ + sha256 前 12 位），用于按内容去重
def new_memory_id(content: str, project: str = "") -> str:
    identity = project + "\0" + content if project else content
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"mem_{digest[:12]}"


@dataclass
class MemoryEntry:
    id: str
    content: str
    kind: MemoryKind
    project: str = ""                # 空串表示全局记忆，跨项目可见
    importance: float = 0.5          # 0.0~1.0，模型自评的重要程度
    tags: list[str] = field(default_factory=list)
    status: MemoryStatus = "active"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    last_accessed_at: str = field(default_factory=_now)
    source: str = ""
    source_run_id: str = ""
    verified_at: str | None = None
    expires_at: str | None = None
    confidence: float = 0.5
    policy_tags: list[str] = field(default_factory=list)
    schema_version: int = 1

    # 将条目序列化为普通 dict，供 sqlite 行与 LLM 输入使用
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # 从 dict 还原条目，容忍缺字段与类型漂移
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryEntry:
        tags = data.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        return cls(
            id=str(data["id"]),
            content=str(data.get("content", "")),
            kind=data.get("kind", "fact"),
            project=str(data.get("project", "")),
            importance=float(data.get("importance", 0.5)),
            tags=[str(t) for t in tags],
            status=data.get("status", "active"),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            last_accessed_at=str(data.get("last_accessed_at", "")),
            source=str(data.get("source") or ""),
            source_run_id=str(data.get("source_run_id") or ""),
            verified_at=data.get("verified_at"),
            expires_at=data.get("expires_at"),
            confidence=max(0.0, min(1.0, float(data.get("confidence", 0.5)))),
            policy_tags=[str(tag) for tag in data.get("policy_tags", [])],
            schema_version=int(data.get("schema_version", 1)),
        )
