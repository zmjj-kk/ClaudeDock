from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from claudedock.core.agents.loader import AgentProfile, AgentProfileLoader
from claudedock.core.bus.events import SubagentFinishedEvent, SubagentStartedEvent
from claudedock.core.compact.compactor import Compactor
from claudedock.core.compact.tool_result_store import ReadToolResultTool, ToolResultStore
from claudedock.core.config import CompactionConfig, ToolExecutionConfig
from claudedock.core.context import ExecutionBudget, ExecutionContext
from claudedock.core.events.bus import EventBus
from claudedock.core.events.writer import EventWriter
from claudedock.core.journal import RunJournal, current_journal
from claudedock.core.loop import AgentLoop
from claudedock.core.runs import new_run_id
from claudedock.core.security.capabilities import Capability
from claudedock.core.subagent.handoff import ResultSummary, TaskHandoff, verify_result_files
from claudedock.core.subagent.registry import BackgroundTaskRegistry
from claudedock.core.supervisor import RunSupervisor
from claudedock.core.task.manager import TaskManager
from claudedock.core.tools.base import BaseTool, ToolResult
from claudedock.core.tools.builtin.bash import BashTool
from claudedock.core.tools.builtin.list_dir import ListDirTool
from claudedock.core.tools.builtin.read_file import ReadFileTool
from claudedock.core.tools.builtin.task_create import TaskCreateTool
from claudedock.core.tools.builtin.task_get import TaskGetTool
from claudedock.core.tools.builtin.task_list import TaskListTool
from claudedock.core.tools.builtin.task_update import TaskUpdateTool
from claudedock.core.tools.builtin.write_file import WriteFileTool
from claudedock.core.tools.discovery import ToolSearchTool
from claudedock.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from claudedock.core.llm.base import LLMProvider
    from claudedock.core.permissions.manager import PermissionManager

# 最大允许的 agent 嵌套深度：根 agent=0，最多向下派生到 depth=2（depth 2 不可再派生）
_MAX_DEPTH = 2

_profile_loader = AgentProfileLoader()


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SpawnAgentParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    description: str
    prompt: str
    run_in_background: bool = False
    subagent_type: str = ""
    handoff: dict[str, Any] = {}


# 在隔离的冷启动上下文中派生子 agent，支持前台阻塞和后台并行两种模式
class SpawnAgentTool(BaseTool):
    capabilities = frozenset({Capability.WRITE})
    name = "spawn_agent"
    description = (
        "Spawn an isolated sub-agent to handle a self-contained sub-task. "
        "The sub-agent starts with a clean context containing only the provided prompt — "
        "it does not inherit the current conversation history. "
        "Use run_in_background=true to run in parallel; retrieve result later with agent_result."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "3-5 word task description shown in progress display",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "Complete task description including all context the sub-agent needs. "
                    "The sub-agent cannot see the parent conversation, so be explicit."
                ),
            },
            "run_in_background": {
                "type": "boolean",
                "description": "When true, returns immediately with a run_id; use agent_result to poll.",  # noqa: E501
            },
            "subagent_type": {
                "type": "string",
                "description": "Agent role profile (planner/executor/reviewer). Leave empty for default.",  # noqa: E501
            },
            "handoff": {
                "type": "object",
                "properties": {
                    "expected_output": {"type": "string", "maxLength": 2048},
                    "relevant_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 32,
                    },
                    "known_constraints": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 16,
                    },
                    "evidence_refs": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
                },
                "additionalProperties": False,
            },
        },
        "required": ["description", "prompt"],
    }
    params_model = SpawnAgentParams

    # 构造 SpawnAgentTool；depth=0 表示根 agent，最大嵌套深度由 _MAX_DEPTH 控制
    def __init__(
        self,
        provider: LLMProvider,
        parent_bus: EventBus,
        parent_run_id: str,
        permission_manager: PermissionManager | None,
        max_steps: int,
        task_registry: BackgroundTaskRegistry,
        runs_dir: Path,
        session_id: str,
        depth: int = 0,
        provider_factory: Callable[[str], LLMProvider | None] | None = None,
        inherit_tools: Sequence[BaseTool] = (),
        compact_threshold: float = 0.0,
        task_manager: TaskManager | None = None,
        parent_tools: set[str] | None = None,
        budget: ExecutionBudget | None = None,
        tool_slots: asyncio.Semaphore | None = None,
        supervisor: RunSupervisor | None = None,
        compaction_config: CompactionConfig | None = None,
        tool_execution_config: ToolExecutionConfig | None = None,
        trusted_constraints: list[str] | None = None,
    ) -> None:
        self._provider = provider
        self._parent_bus = parent_bus
        self._parent_run_id = parent_run_id
        self._permission_manager = permission_manager
        self._max_steps = max_steps
        self._task_registry = task_registry
        self._runs_dir = runs_dir
        self._session_id = session_id
        self._depth = depth
        self._provider_factory = provider_factory
        self._inherit_tools = list(inherit_tools)
        self._compact_threshold = compact_threshold
        self._task_manager = task_manager
        self._parent_tools = parent_tools
        self._budget = budget
        self._tool_slots = tool_slots
        self._supervisor = supervisor
        self._compaction_config = compaction_config or CompactionConfig()
        self._tool_execution_config = tool_execution_config or ToolExecutionConfig()
        self._trusted_constraints = list(trusted_constraints or [])

    # 派生子 agent，前台时阻塞直到完成并返回结果，后台时立即返回 run_id
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = SpawnAgentParams.model_validate(params)

        if self._depth >= _MAX_DEPTH:
            return ToolResult(
                content=(
                    f"Subagent nesting limit ({_MAX_DEPTH}) reached; "
                    "cannot spawn further subagents."
                ),
                is_error=True,
                error_type="runtime_error",
            )

        profile: AgentProfile | None = None
        if p.subagent_type:
            profile = _profile_loader.load(p.subagent_type)

        child_run_id = new_run_id()
        child_context = ExecutionContext(
            run_id=child_run_id,
            goal=p.prompt,
            max_steps=self._max_steps,
            budget=self._budget,
            parent_run_id=self._parent_run_id,
            system_prompt_override=profile.system_prompt if profile else None,
            prompt_cache_enabled=self._compaction_config.prompt_cache_enabled,
        )

        child_bus = EventBus()

        # 将子 bus 所有事件桥接到父 bus，TUI 据此渲染嵌套进度
        async def _bridge(event: BaseModel) -> None:
            await self._parent_bus.publish(event)

        child_bus.subscribe(_bridge)

        child_registry = self._build_child_registry(
            child_bus,
            child_run_id,
            profile,
            task_goal=p.prompt,
        )

        # 角色配置了 model 时按角色派生 provider；工厂缺失或构造失败则沿用父 provider
        child_provider = self._provider
        if profile is not None and profile.model and self._provider_factory is not None:
            built = self._provider_factory(profile.model)
            if built is not None:
                child_provider = built

        child_run_path = self._runs_dir / child_run_id
        child_run_path.mkdir(parents=True, exist_ok=True)

        child_journal = RunJournal(child_run_path / "checkpoint.json", child_run_id, p.prompt)
        child_journal.data["parent_run_id"] = self._parent_run_id
        child_journal.save()
        parent_journal = current_journal.get()
        if parent_journal is not None:
            parent_journal.data.setdefault("children", []).append(child_run_id)
            parent_journal.save()

        # 达到压缩阈值时为子 loop 构造独立 compactor，防止长任务撑爆上下文
        comp = self._compaction_config
        compactor = Compactor(
            child_bus,
            child_run_path,
            self._session_id,
            recent_turns=comp.recent_turns_min if comp.enabled else 0,
            min_steps_between=comp.min_steps_between,
            min_growth_ratio=comp.min_growth_ratio,
            max_failures=comp.max_consecutive_failures,
            enforce_gain=comp.enabled,
            target_low_watermark=comp.low_watermark,
        )
        result_store = None
        if comp.tool_result_budget_enabled:
            result_store = ToolResultStore(
                child_run_path / "tool-results",
                max_bytes=comp.tool_result_max_bytes,
                preview_chars=comp.tool_preview_max_chars,
                quota_bytes=comp.tool_result_quota_bytes,
                retention_days=comp.tool_result_retention_days,
            )
            allowed = set(profile.allowed_tools) if profile and profile.allowed_tools else None
            if (self._parent_tools is None or "read_tool_result" in self._parent_tools) and (
                allowed is None or "read_tool_result" in allowed
            ):
                child_registry.register(ReadToolResultTool(result_store))

        allowed_names = set(profile.allowed_tools) if profile and profile.allowed_tools else None
        if (
            comp.tool_schema_deferred
            and (self._parent_tools is None or "tool_search" in self._parent_tools)
            and (allowed_names is None or "tool_search" in allowed_names)
        ):
            child_registry.register(ToolSearchTool(child_registry))
            child_registry.enable_deferred(
                {
                    "tool_search",
                    "read_file",
                    "write_file",
                    "list_dir",
                    "bash",
                    "read_tool_result",
                    "spawn_agent",
                    "agent_result",
                    "task_create",
                    "task_update",
                    "task_list",
                    "task_get",
                    "use_skill",
                    "read_skill_resource",
                }
            )

        if self._compaction_config.structured_handoff_enabled:
            handoff = TaskHandoff.build(
                p.prompt,
                p.handoff,
                self._trusted_constraints,
                [str(item["name"]) for item in child_registry.manifest()],
                self._parent_run_id,
                budget={
                    "remaining_tokens": max(0, self._budget.max_tokens - self._budget.tokens),
                    "remaining_model_calls": max(
                        0,
                        self._budget.max_model_calls - self._budget.model_calls,
                    ),
                    "remaining_seconds": max(0.0, self._budget.deadline - time.monotonic()),
                }
                if self._budget is not None
                else {},
            )
            child_context.messages = [
                {
                    "role": "user",
                    "content": p.prompt + "\n\nTask handoff data:\n" + handoff.render(),
                }
            ]
            child_context.audit_messages = [dict(message) for message in child_context.messages]
            child_context.user_requests = [p.prompt] + handoff.known_constraints

        child_loop = AgentLoop(
            child_provider,
            child_registry,
            child_bus,
            permission_manager=self._permission_manager,
            compactor=compactor,
            compact_threshold=self._compact_threshold,
            session_id=self._session_id,
            tool_slots=self._tool_slots,
            compaction_config=comp,
            tool_execution_config=self._tool_execution_config,
            result_store=result_store,
        )

        await self._parent_bus.publish(
            SubagentStartedEvent(
                run_id=child_run_id,
                parent_run_id=self._parent_run_id,
                description=p.description,
                ts=_now(),
            )
        )

        if p.run_in_background:
            work = self._run_background(
                child_loop, child_context, child_bus, child_run_path, child_run_id
            )
            task: asyncio.Task[None] = (
                self._supervisor.spawn(work, key=child_run_id)
                if self._supervisor is not None
                else asyncio.create_task(work)
            )
            self._task_registry.register(child_run_id, task, child_context)
            return ToolResult(
                content=(
                    f"Subagent started in background. run_id={child_run_id}. "
                    f"Use agent_result(run_id='{child_run_id}') to retrieve result."
                )
            )

        async with EventWriter(child_run_path / "events.jsonl") as writer:
            writer.subscribe(child_bus)
            await self._run_child(child_loop, child_context, child_run_path)

        await self._parent_bus.publish(
            SubagentFinishedEvent(
                run_id=child_run_id,
                parent_run_id=self._parent_run_id,
                status=child_context.status,
                ts=_now(),
            )
        )

        if child_context.result_summary is not None:
            return ToolResult(
                json.dumps(
                    verify_result_files(child_context.result_summary, Path.cwd()),
                    ensure_ascii=False,
                ),
                is_error=child_context.status != "success",
            )
        if child_context.status == "success":
            return ToolResult(
                content=child_context.result or "Subagent completed with no text output."
            )
        return ToolResult(
            content=(
                child_context.result
                or f"Subagent failed (status={child_context.status}, reason={child_context.reason})"
            ),
            is_error=True,
            error_type="runtime_error",
        )

    # 每个子任务使用独立恢复日志，避免继承父 ContextVar 后污染操作记录
    async def _run_child(self, loop: AgentLoop, context: ExecutionContext, path: Path) -> None:
        journal = RunJournal(path / "checkpoint.json", context.run_id, context.goal)
        token = current_journal.set(journal)
        try:
            journal.checkpoint(context.messages)
            await loop.run(context)
        except asyncio.CancelledError:
            context.mark_failed("cancelled")
            await self._task_registry.cancel_children(context.run_id)
            raise
        finally:
            if self._compaction_config.structured_handoff_enabled:
                summary = ResultSummary.build(
                    context.run_id,
                    context.status,
                    context.result,
                    journal.data.get("operations", {}),
                    context.audit_messages,
                    Path.cwd(),
                )
                context.result_summary = summary.to_dict()
                journal.data["result_summary"] = context.result_summary
            current_journal.reset(token)
            journal.checkpoint(
                context.messages, "completed" if context.status == "success" else "interrupted"
            )

    # 后台任务协程：写事件文件，运行 loop，最终无论成败（含取消）都发布完成事件
    async def _run_background(
        self,
        loop: AgentLoop,
        context: ExecutionContext,
        bus: EventBus,
        run_path: Path,
        run_id: str,
    ) -> None:
        try:
            async with EventWriter(run_path / "events.jsonl") as writer:
                writer.subscribe(bus)
                await self._run_child(loop, context, run_path)
        finally:
            await self._parent_bus.publish(
                SubagentFinishedEvent(
                    run_id=run_id,
                    parent_run_id=self._parent_run_id,
                    status=context.status,
                    ts=_now(),
                )
            )

    # 构造子 registry；基于角色配置过滤工具，深度允许时注册嵌套 SpawnAgentTool
    def _build_child_registry(
        self,
        child_bus: EventBus,
        child_run_id: str,
        profile: AgentProfile | None,
        task_goal: str = "",
    ) -> ToolRegistry:
        allowed: set[str] | None = (
            set(profile.allowed_tools) if profile and profile.allowed_tools else None
        )

        def _allowed(name: str) -> bool:
            if self._parent_tools is not None and name not in self._parent_tools:
                return False
            return allowed is None or name in allowed

        registry = ToolRegistry()
        _all_tools = [
            ReadFileTool(),
            BashTool(),
            WriteFileTool(),
            ListDirTool(),
        ]
        for t in _all_tools:
            if _allowed(t.name):
                registry.register(t)

        # 子 agent 默认共享父 run 的任务列表；未注入时才按子 run 独立建目录
        child_task_manager = self._task_manager or TaskManager(
            self._runs_dir / child_run_id / ".tasks"
        )
        for t in [
            TaskCreateTool(child_task_manager),
            TaskUpdateTool(child_task_manager),
            TaskListTool(child_task_manager),
            TaskGetTool(child_task_manager),
        ]:
            if _allowed(t.name):
                registry.register(t)

        # 透传父 registry 的可继承工具（RAG / 记忆 / 技能），受角色 allowed_tools 约束
        for t in self._inherit_tools:
            if _allowed(t.name):
                registry.register(t)

        if self._depth + 1 < _MAX_DEPTH:
            nested = SpawnAgentTool(
                provider=self._provider,
                parent_bus=child_bus,
                parent_run_id=child_run_id,
                permission_manager=self._permission_manager,
                max_steps=self._max_steps,
                task_registry=self._task_registry,
                runs_dir=self._runs_dir,
                session_id=self._session_id,
                depth=self._depth + 1,
                provider_factory=self._provider_factory,
                inherit_tools=self._inherit_tools,
                compact_threshold=self._compact_threshold,
                task_manager=self._task_manager,
                parent_tools={
                    name
                    for name in (
                        self._parent_tools or set(str(item["name"]) for item in registry.manifest())
                    )
                    if _allowed(name)
                },
                budget=self._budget,
                tool_slots=self._tool_slots,
                supervisor=self._supervisor,
                compaction_config=self._compaction_config,
                tool_execution_config=self._tool_execution_config,
                trusted_constraints=self._trusted_constraints + [task_goal],
            )
            if _allowed("spawn_agent"):
                registry.register(nested)
            if _allowed("agent_result"):
                registry.register(AgentResultTool(self._task_registry))

        return registry


class AgentResultParams(BaseModel):
    run_id: str


# 查询后台 subagent 的执行状态和最终结果
class AgentResultTool(BaseTool):
    capabilities = frozenset({Capability.WRITE})
    name = "agent_result"
    description = (
        "Retrieve the result of a background sub-agent previously started with spawn_agent. "
        "Returns 'still running' if the sub-agent has not yet completed."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "The run_id returned by spawn_agent(run_in_background=true)",
            },
        },
        "required": ["run_id"],
    }
    params_model = AgentResultParams
    # 只读工具：runtime_error（如后台任务尚未就绪）可安全重试，不会产生副作用
    retryable_errors = frozenset({"rate_limited", "runtime_error"})

    # 初始化，持有共享的后台任务注册表
    def __init__(self, task_registry: BackgroundTaskRegistry) -> None:
        self._task_registry = task_registry

    # 查询指定 run_id 的后台任务状态，返回结果或错误；完成后从注册表移除以释放内存
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = AgentResultParams.model_validate(params)
        entry = self._task_registry.get(p.run_id)
        if entry is None:
            return ToolResult(
                content=f"Unknown run_id: {p.run_id}. Only background subagents can be queried.",
                is_error=True,
                error_type="runtime_error",
            )
        task, context = entry
        if not task.done():
            return ToolResult(content="still running")
        self._task_registry.remove(p.run_id)
        if task.cancelled():
            return ToolResult(
                content="Subagent was cancelled.", is_error=True, error_type="runtime_error"
            )
        exc = task.exception()
        if exc is not None:
            return ToolResult(
                content=f"Subagent raised an exception: {exc}",
                is_error=True,
                error_type="runtime_error",
            )
        if context.result_summary is not None:
            return ToolResult(
                json.dumps(
                    verify_result_files(context.result_summary, Path.cwd()),
                    ensure_ascii=False,
                ),
                is_error=context.status != "success",
            )
        if context.status != "success":
            return ToolResult(
                content=(
                    context.result
                    or f"Subagent failed (status={context.status}, "
                    f"reason={context.reason or 'unknown'})"
                ),
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(content=context.result or "Subagent completed with no text result.")
