"""
S5 permission flow integration tests.

No daemon subprocess needed — uses AgentRunner in-process with a mock LLM
provider and the real PermissionManager. BashTool runs real subprocesses, so
commands must be safe (echo, true).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from claudedock.core.config import ClaudeDockConfig
from claudedock.core.events.bus import EventBus
from claudedock.core.llm.types import LlmResponse, ToolCallBlock
from claudedock.core.permissions.manager import PermissionManager
from claudedock.core.runner import AgentRunner
from claudedock.core.tools.builtin.bash import BashTool


# 仅隔离审批流程测试使用非沙箱执行，沙箱边界由独立回归测试验证
@pytest.fixture(autouse=True)
def permission_shell_fixture(monkeypatch):
    monkeypatch.setattr(BashTool, "_argv", lambda self, command, timeout: ["/bin/bash", "-c", command])


# ── stub providers ────────────────────────────────────────────────────────────


class _SingleBashProvider:
    """Step 1: bash tool call. Step 2: end_turn."""

    def __init__(self, command: str = "echo hello") -> None:
        self._command = command
        self._step = 0

    async def chat(
        self,
        messages: list[dict],
        tool_schemas: list[dict],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        self._step += 1
        if self._step == 1:
            tc = ToolCallBlock(id="tc1", name="bash", input={"command": self._command})
            return LlmResponse(stop_reason="tool_use", tool_calls=[tc])
        return LlmResponse(stop_reason="end_turn", text="done")


class _TwoBashProvider:
    """Step 1+2: two separate bash calls. Step 3: end_turn."""

    def __init__(self) -> None:
        self._step = 0

    async def chat(
        self,
        messages: list[dict],
        tool_schemas: list[dict],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        self._step += 1
        if self._step == 1:
            tc = ToolCallBlock(id="tc1", name="bash", input={"command": "echo first"})
            return LlmResponse(stop_reason="tool_use", tool_calls=[tc])
        if self._step == 2:
            tc = ToolCallBlock(id="tc2", name="bash", input={"command": "echo second"})
            return LlmResponse(stop_reason="tool_use", tool_calls=[tc])
        return LlmResponse(stop_reason="end_turn", text="done")


# ── helper ────────────────────────────────────────────────────────────────────


def _runner(
    provider: object,
    bus: EventBus,
    manager: PermissionManager,
    tmp_path: Path,
    max_steps: int = 10,
) -> AgentRunner:
    config = ClaudeDockConfig()
    config.agent.max_steps = max_steps
    return AgentRunner(
        config,
        bus=bus,
        provider=provider,  # type: ignore[arg-type]
        permission_manager=manager,
        runs_dir=tmp_path / "runs",
    )


# ── tests ─────────────────────────────────────────────────────────────────────


# 功能：验证 allow_once 决策后工具正常执行并写入 tool.call_finished 事件
# 设计：在 permission.requested 事件到达时同步调用 manager.respond("allow_once")；
#       Future 在同一 event-loop turn 内解决，工具随后执行；断言 tool.call_finished 存在且 tool.call_failed 不存在
async def test_permission_allow_once_tool_executes(tmp_path: Path) -> None:
    manager = PermissionManager()
    bus = EventBus()
    event_types: list[str] = []

    async def collect(e: BaseModel) -> None:
        t = getattr(e, "type", "")
        event_types.append(t)
        if t == "permission.requested":
            manager.respond(getattr(e, "tool_use_id", ""), "allow_once", approval_token=getattr(e, "approval_token", ""))

    bus.subscribe(collect)
    outcome = await _runner(_SingleBashProvider(), bus, manager, tmp_path).run_and_capture(
        "run bash"
    )

    assert "permission.requested" in event_types
    assert "tool.call_finished" in event_types
    assert "tool.call_failed" not in event_types
    assert outcome.status == "success"


# 功能：验证 deny_once 决策后工具不执行，事件流中出现 permission_denied 错误
# 设计：在 permission.requested 时 respond("deny_once")；断言 tool.call_failed 的 error_class 为
#       "permission_denied"，且 tool.call_finished 不出现，确认工具从未被调用
async def test_permission_deny_once_tool_not_executed(tmp_path: Path) -> None:
    manager = PermissionManager()
    bus = EventBus()
    event_types: list[str] = []
    failed_events: list[BaseModel] = []

    async def collect(e: BaseModel) -> None:
        t = getattr(e, "type", "")
        event_types.append(t)
        if t == "permission.requested":
            manager.respond(getattr(e, "tool_use_id", ""), "deny_once", approval_token=getattr(e, "approval_token", ""))
        if t == "tool.call_failed":
            failed_events.append(e)

    bus.subscribe(collect)
    await _runner(_SingleBashProvider(), bus, manager, tmp_path).run_and_capture("run bash")

    assert "permission.requested" in event_types
    assert "tool.call_failed" in event_types
    assert "tool.call_finished" not in event_types
    assert getattr(failed_events[0], "error_class", None) == "permission_denied"


# 功能：验证 always_allow 后不同命令仍触发独立审批
# 设计：两步不同参数的 bash 调用，确认每一步均发出审批事件，防止权限扩大
async def test_always_allow_cached_within_session(tmp_path: Path) -> None:
    manager = PermissionManager()
    bus = EventBus()
    perm_requested_count = 0

    async def collect(e: BaseModel) -> None:
        nonlocal perm_requested_count
        if getattr(e, "type", "") == "permission.requested":
            perm_requested_count += 1
            manager.respond(getattr(e, "tool_use_id", ""), "always_allow", approval_token=getattr(e, "approval_token", ""))

    bus.subscribe(collect)
    outcome = await _runner(_TwoBashProvider(), bus, manager, tmp_path).run_and_capture(
        "run two bash commands"
    )

    assert perm_requested_count == 2
    assert outcome.status == "success"
