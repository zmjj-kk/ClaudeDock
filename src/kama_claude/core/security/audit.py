"""审计日志：记录安全相关事件（权限决策、工具调用、配置变更）。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from kama_claude.core.security.redaction import redact

logger = logging.getLogger(__name__)


class AuditEventType(StrEnum):
    """审计事件类型"""

    PERMISSION_GRANT = "permission_grant"
    PERMISSION_DENY = "permission_deny"
    TOOL_CALL = "tool_call"
    CONFIG_CHANGE = "config_change"
    SESSION_CREATE = "session_create"
    SESSION_CLOSE = "session_close"
    API_KEY_ROTATION = "api_key_rotation"


@dataclass
class AuditEvent:
    """审计事件记录"""

    event_type: AuditEventType
    timestamp: str
    session_id: str
    user_id: str | None
    details: dict[str, Any]
    outcome: str  # "success" | "denied" | "error"

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式（用于 JSON 序列化）"""
        return {
            "type": self.event_type,
            "ts": self.timestamp,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "details": self.details,
            "outcome": self.outcome,
        }


class AuditLogger:
    """审计日志记录器：将安全事件持久化到文件"""

    def __init__(self, log_file: Path | None = None) -> None:
        """初始化审计日志记录器

        Args:
            log_file: 审计日志文件路径（默认 ~/.kama/audit.log）
        """
        self._log_file = log_file or Path.home() / ".kama" / "audit.log"
        self._log_file.parent.mkdir(parents=True, exist_ok=True)

    # 记录审计事件
    def log(self, event: AuditEvent) -> None:
        """记录审计事件到文件和系统日志"""
        # 写入专用审计日志文件（JSON Lines 格式）
        try:
            import json

            with self._log_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(redact(event.to_dict()), ensure_ascii=False) + "\n")
        except Exception:
            logger.exception("Failed to write audit log")

        # 同时输出到系统日志（便于实时监控）
        logger.info(
            "Audit event",
            extra={
                "audit_type": event.event_type,
                "session_id": event.session_id,
                "outcome": event.outcome,
            },
        )

    # 记录权限决策
    def log_permission_decision(
        self,
        session_id: str,
        tool_name: str,
        decision: str,
        reason: str = "",
        user_id: str | None = None,
    ) -> None:
        """记录工具权限决策"""
        event_type = (
            AuditEventType.PERMISSION_GRANT
            if decision == "allow"
            else AuditEventType.PERMISSION_DENY
        )
        event = AuditEvent(
            event_type=event_type,
            timestamp=datetime.now(UTC).isoformat(),
            session_id=session_id,
            user_id=user_id,
            details={"tool": tool_name, "reason": reason},
            outcome=decision,
        )
        self.log(event)

    # 记录工具调用
    def log_tool_call(
        self,
        session_id: str,
        tool_name: str,
        params: dict[str, Any],
        outcome: str,
        user_id: str | None = None,
    ) -> None:
        """记录工具调用（参数已脱敏）"""
        from kama_claude.core.security.secrets import SecretManager

        event = AuditEvent(
            event_type=AuditEventType.TOOL_CALL,
            timestamp=datetime.now(UTC).isoformat(),
            session_id=session_id,
            user_id=user_id,
            details={
                "tool": tool_name,
                "params": SecretManager.sanitize(params),
            },
            outcome=outcome,
        )
        self.log(event)

    # 记录配置变更
    def log_config_change(
        self,
        session_id: str,
        config_key: str,
        old_value: Any,
        new_value: Any,
        user_id: str | None = None,
    ) -> None:
        """记录配置变更"""
        from kama_claude.core.security.secrets import SecretManager

        event = AuditEvent(
            event_type=AuditEventType.CONFIG_CHANGE,
            timestamp=datetime.now(UTC).isoformat(),
            session_id=session_id,
            user_id=user_id,
            details={
                "key": config_key,
                "old_value": SecretManager.sanitize(old_value),
                "new_value": SecretManager.sanitize(new_value),
            },
            outcome="success",
        )
        self.log(event)
