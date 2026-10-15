from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Discriminator


class CoreStartedEvent(BaseModel):
    type: Literal["core.started"] = "core.started"
    listen_addr: str  # e.g. "127.0.0.1:7437"
    version: str


class RunStartedEvent(BaseModel):
    session_id: str | None = None
    type: Literal["run.started"] = "run.started"
    run_id: str
    goal: str
    ts: str  # ISO 8601


class RunFinishedEvent(BaseModel):
    type: Literal["run.finished"] = "run.finished"
    run_id: str
    status: str  # "success" | "failed"
    reason: str | None = None  # "exceeded_max_steps" | "cancelled" | "llm_error" | ...
    steps: int
    ts: str


class StepStartedEvent(BaseModel):
    type: Literal["step.started"] = "step.started"
    run_id: str
    step: int
    ts: str


class StepFinishedEvent(BaseModel):
    type: Literal["step.finished"] = "step.finished"
    run_id: str
    step: int
    ts: str


class ToolCallStartedEvent(BaseModel):
    type: Literal["tool.call_started"] = "tool.call_started"
    run_id: str
    tool_use_id: str
    tool_name: str
    params: dict[str, Any]
    ts: str


class ToolCallFinishedEvent(BaseModel):
    type: Literal["tool.call_finished"] = "tool.call_finished"
    run_id: str
    tool_use_id: str
    tool_name: str
    elapsed_ms: int
    output: str = ""  # tool result content, for TUI display
    ts: str


class ToolCallFailedEvent(BaseModel):
    type: Literal["tool.call_failed"] = "tool.call_failed"
    run_id: str
    tool_use_id: str
    tool_name: str
    # "runtime_error" | "timeout" | "schema_error" | "permission_denied" | "rate_limited"
    error_class: str
    error_message: str
    elapsed_ms: int
    attempt: int = 1  # 1=first attempt, 2=first retry, 3=second retry
    ts: str


class LlmTokenEvent(BaseModel):
    type: Literal["llm.token"] = "llm.token"
    run_id: str
    token: str
    ts: str


class LlmUsageEvent(BaseModel):
    context_window: int | None = None
    type: Literal["llm.usage"] = "llm.usage"
    run_id: str
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    context_pct: float = 0.0
    ts: str


class LlmModelSelectedEvent(BaseModel):
    type: Literal["llm.model_selected"] = "llm.model_selected"
    run_id: str
    model: str
    strategy: str  # "static" | "rule_based" | "cost_budget"
    ts: str


class LogLineEvent(BaseModel):
    type: Literal["log.line"] = "log.line"
    run_id: str
    level: str  # "DEBUG" | "INFO" | "WARNING" | "ERROR"
    source: str
    message: str
    ts: str


class SessionCreatedEvent(BaseModel):
    type: Literal["session.created"] = "session.created"
    session_id: str
    mode: str
    ts: str


class SessionMessageReceivedEvent(BaseModel):
    type: Literal["session.message_received"] = "session.message_received"
    session_id: str
    content: str
    ts: str


class SessionWaitingForInputEvent(BaseModel):
    type: Literal["session.waiting_for_input"] = "session.waiting_for_input"
    session_id: str
    last_run_id: str
    ts: str


class SessionResumedEvent(BaseModel):
    type: Literal["session.resumed"] = "session.resumed"
    session_id: str
    ts: str


class SessionClosedEvent(BaseModel):
    type: Literal["session.closed"] = "session.closed"
    session_id: str
    ts: str


class ContextSourcesLoadedEvent(BaseModel):
    type: Literal["context.sources_loaded"] = "context.sources_loaded"
    session_id: str = ""
    run_id: str
    sources: list[dict[str, Any]]
    diagnostics: list[str]
    ts: str


class ContextMemoryUpdatedEvent(BaseModel):
    type: Literal["context.memory_updated"] = "context.memory_updated"
    session_id: str
    run_id: str
    revision: int
    source_count: int
    ts: str


class ContextCacheUsageEvent(BaseModel):
    type: Literal["context.cache_usage"] = "context.cache_usage"
    session_id: str = ""
    run_id: str
    stable_prefix_hash: str
    cache_read: int
    cache_create: int
    ts: str


class ContextBudgetCheckedEvent(BaseModel):
    type: Literal["context.budget_checked"] = "context.budget_checked"
    run_id: str
    session_id: str = ""
    window: int | None
    input_tokens: int
    reserved_output: int
    safety_margin: int
    system_tokens: int
    tools_tokens: int
    history_tokens: int
    over_limit: bool
    mode: str
    estimate_method: str
    ts: str


class ContextPrunedEvent(BaseModel):
    type: Literal["context.pruned"] = "context.pruned"
    run_id: str
    count: int
    saved_tokens: int
    ts: str


class ContextCompactionStartedEvent(BaseModel):
    type: Literal["context.compaction_started"] = "context.compaction_started"
    run_id: str
    session_id: str
    revision: int
    pre_tokens: int
    strategy: str = "rolling"
    target_low_watermark: float = 0.5
    ts: str


class ContextCompactionFailedEvent(BaseModel):
    type: Literal["context.compaction_failed"] = "context.compaction_failed"
    session_id: str = ""
    run_id: str
    consecutive_failures: int
    circuit_open: bool
    reason: str
    elapsed_ms: int = 0
    ts: str


class ContextCompactedEvent(BaseModel):
    type: Literal["context.compacted"] = "context.compacted"
    revision: int = 0
    retained_messages: int = 0
    projection_tokens: int | None = None
    estimate_method: str = "utf8_upper_bound"
    elapsed_ms: int = 0
    session_id: str
    run_id: str
    original_tokens: int
    summary_tokens: int
    ts: str


class SecurityDecisionEvent(BaseModel):
    type: Literal["security.decision"] = "security.decision"
    run_id: str
    session_id: str
    tool_use_id: str
    operation_id: str
    principal_id: str
    server_id: str
    argument_hash: str
    policy_revision: str
    action: str
    reason_code: str
    elapsed_ms: int = 0
    ts: str


class PermissionRequestedEvent(BaseModel):
    approval_token: str = ""
    approval_scope: str = ""
    policy_revision: str = ""
    type: Literal["permission.requested"] = "permission.requested"
    run_id: str
    tool_use_id: str
    tool_name: str
    params: dict[str, Any]
    param_preview: str
    session_id: str
    ts: str


class PermissionGrantedEvent(BaseModel):
    type: Literal["permission.granted"] = "permission.granted"
    run_id: str
    tool_use_id: str
    # "allow_once" | "always_allow" | "auto_allow"
    decision: str
    ts: str


class PermissionDeniedEvent(BaseModel):
    type: Literal["permission.denied"] = "permission.denied"
    run_id: str
    tool_use_id: str
    # "deny_once" | "always_deny" | "auto_deny"
    decision: str
    ts: str


class SubagentStartedEvent(BaseModel):
    type: Literal["subagent.started"] = "subagent.started"
    run_id: str  # 子 agent run_id
    parent_run_id: str
    description: str
    ts: str


class SubagentFinishedEvent(BaseModel):
    type: Literal["subagent.finished"] = "subagent.finished"
    run_id: str
    parent_run_id: str
    status: str  # "success" | "failed"
    ts: str


class SkillInvokedEvent(BaseModel):
    type: Literal["skill.invoked"] = "skill.invoked"
    skill_name: str
    arguments: str
    run_id: str
    ts: str


# 根据 type 字段决定事件类型的判别联合
Event = Annotated[
    CoreStartedEvent
    | RunStartedEvent
    | RunFinishedEvent
    | StepStartedEvent
    | StepFinishedEvent
    | ToolCallStartedEvent
    | ToolCallFinishedEvent
    | ToolCallFailedEvent
    | LlmTokenEvent
    | LlmUsageEvent
    | LlmModelSelectedEvent
    | LogLineEvent
    | SessionCreatedEvent
    | SessionMessageReceivedEvent
    | SessionWaitingForInputEvent
    | SessionResumedEvent
    | SessionClosedEvent
    | ContextSourcesLoadedEvent
    | ContextMemoryUpdatedEvent
    | ContextCacheUsageEvent
    | ContextBudgetCheckedEvent
    | ContextPrunedEvent
    | ContextCompactionStartedEvent
    | ContextCompactionFailedEvent
    | ContextCompactedEvent
    | SecurityDecisionEvent
    | PermissionRequestedEvent
    | PermissionGrantedEvent
    | PermissionDeniedEvent
    | SubagentStartedEvent
    | SubagentFinishedEvent
    | SkillInvokedEvent,
    Discriminator("type"),
]
