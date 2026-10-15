from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Discriminator, Field

from kama_claude.core.session.model import SessionMode, SessionStatus


class PingCommand(BaseModel):
    type: Literal["core.ping"] = "core.ping"
    client: str


class PongResult(BaseModel):
    server_version: str
    uptime_ms: int
    received_at: str  # ISO 8601


class AgentRunCommand(BaseModel):
    type: Literal["agent.run"] = "agent.run"
    goal: str


class AgentRunResult(BaseModel):
    run_id: str


class EventSubscribeCommand(BaseModel):
    type: Literal["event.subscribe"] = "event.subscribe"
    topics: list[str]  # fnmatch 模式，如 ["step.*", "tool.*"]
    scope: str = "global"  # "global" | "run:<run_id>"
    after_sequence: int = Field(default=0, ge=0)
    replay_from_run: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_-]{1,80}$"
    )  # 设置则先从 events.jsonl 回放历史再接实时流


class EventSubscribeResult(BaseModel):
    subscription_id: str
    replayed_count: int = 0


class SessionCreateCommand(BaseModel):
    type: Literal["session.create"] = "session.create"
    mode: SessionMode = "chat"
    title: str = ""


class SessionCreateResult(BaseModel):
    session_id: str
    status: SessionStatus


class SessionSendMessageCommand(BaseModel):
    type: Literal["session.send_message"] = "session.send_message"
    session_id: str
    content: str


class SessionSendMessageResult(BaseModel):
    run_id: str


class SessionGetContextCommand(BaseModel):
    type: Literal["session.get_context"] = "session.get_context"
    session_id: str


class SessionGetContextResult(BaseModel):
    notes: str = ""
    sources: list[dict[str, Any]] = []
    projection: dict[str, Any] = {}


class SessionGetHistoryCommand(BaseModel):
    type: Literal["session.get_history"] = "session.get_history"
    session_id: str


class SessionGetHistoryResult(BaseModel):
    messages: list[dict[str, Any]]


class SessionListCommand(BaseModel):
    type: Literal["session.list"] = "session.list"


class SessionListItem(BaseModel):
    id: str
    title: str
    status: SessionStatus
    mode: SessionMode
    created_at: str
    updated_at: str


class SessionListResult(BaseModel):
    sessions: list[SessionListItem]


class SessionCloseCommand(BaseModel):
    type: Literal["session.close"] = "session.close"
    session_id: str


class SessionCloseResult(BaseModel):
    status: SessionStatus


class PermissionRespondCommand(BaseModel):
    approval_token: str = ""
    type: Literal["permission.respond"] = "permission.respond"
    tool_use_id: str
    # "allow_once" | "always_allow" | "deny_once" | "always_deny"
    decision: str


class PermissionRespondResult(BaseModel):
    ok: bool = True


class SessionCompactCommand(BaseModel):
    type: Literal["session.compact"] = "session.compact"
    session_id: str
    focus: str = ""


class SessionCompactResult(BaseModel):
    summary_tokens: int
    saved_tokens: int
    projection_tokens: int | None = None
    revision: int = 0
    retained_messages: int = 0


class MemoryConsolidateCommand(BaseModel):
    type: Literal["memory.consolidate"] = "memory.consolidate"


class MemoryConsolidateResult(BaseModel):
    merged: int = 0
    retired: int = 0


class RunGetCommand(BaseModel):
    type: Literal["run.get"] = "run.get"
    run_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")


class RunGetResult(BaseModel):
    run_id: str
    state: str
    reason: str | None = None


class RunCancelCommand(BaseModel):
    type: Literal["run.cancel"] = "run.cancel"
    run_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")


class RunCancelResult(BaseModel):
    cancelled: bool


# 根据 type 字段决定命令类型的判别联合
Command = Annotated[
    PingCommand
    | AgentRunCommand
    | EventSubscribeCommand
    | SessionCreateCommand
    | SessionSendMessageCommand
    | SessionGetContextCommand
    | SessionGetHistoryCommand
    | SessionListCommand
    | SessionCloseCommand
    | PermissionRespondCommand
    | SessionCompactCommand
    | MemoryConsolidateCommand
    | RunGetCommand
    | RunCancelCommand,
    Discriminator("type"),
]
