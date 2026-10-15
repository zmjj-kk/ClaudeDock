from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.policy import PermissionDecision, ToolPolicy
from kama_claude.core.permissions.rules import Rule
from kama_claude.core.security.context import SecurityContext
from kama_claude.core.security.gate import SecurityGate
from kama_claude.core.tools.builtin.read_file import ReadFileTool
from kama_claude.core.tools.builtin.write_file import WriteFileTool
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.paths import resolve_inside
from kama_claude.core.tools.registry import ToolRegistry


# 构造与真实审批管理器一致的受信不可变上下文
def context(manager: PermissionManager) -> SecurityContext:
    return SecurityContext(
        manager.principal_id,
        manager.project_root,
        "s",
        "r",
        "r:c",
        policy_revision=manager.policy_revision,
        mode=manager.mode,
        deadline=time.monotonic() + 30,
    )


# 功能：验证所有模式下硬拒绝不能被普通授权与 bypass 覆盖
# 设计：对四种模式使用相同写调用，避免只测试单一权限路由
@pytest.mark.parametrize("mode", ["default", "plan", "auto", "bypass"])
async def test_hard_deny_all_modes(tmp_path: Path, mode: str) -> None:
    manager = PermissionManager(
        {"write_file": ToolPolicy(PermissionDecision.DENY)},
        project_root=tmp_path,
        mode=mode,
        bypass_enabled=True,
    )
    registry = ToolRegistry()
    registry.register(WriteFileTool(tmp_path))
    result = await invoke_tool(
        registry,
        ToolCallBlock("c", "write_file", {"path": "x", "content": "x"}),
        EventBus(),
        "r",
        permission_manager=manager,
    )
    assert result.error_type == "permission_denied"
    assert not (tmp_path / "x").exists()


# 功能：验证计划模式只允许宿主明确声明的只读工具
# 设计：使用真实文件工具验证执行结果，防止仅检查标签而漏掉副作用
async def test_plan_read_only(tmp_path: Path) -> None:
    (tmp_path / "x").write_text("safe")
    manager = PermissionManager(project_root=tmp_path, mode="plan")
    registry = ToolRegistry()
    registry.register(ReadFileTool(tmp_path))
    registry.register(WriteFileTool(tmp_path))
    read = await invoke_tool(
        registry,
        ToolCallBlock("a", "read_file", {"path": "x"}),
        EventBus(),
        "r",
        permission_manager=manager,
    )
    write = await invoke_tool(
        registry,
        ToolCallBlock("b", "write_file", {"path": "x", "content": "changed"}),
        EventBus(),
        "r",
        permission_manager=manager,
    )
    assert read.content == "safe"
    assert write.error_type == "permission_denied"
    assert (tmp_path / "x").read_text() == "safe"


# 功能：验证敏感路径、别名与硬链接均不能通过只读或写入工具访问
# 设计：构造真实符号链接与硬链接，覆盖规范化前后路径检查
@pytest.mark.parametrize(
    "target", [".git/config", ".env", ".kama/checkpoint.json", "secret.pem", "CLAUDE.md"]
)
def test_protected_paths(tmp_path: Path, target: str) -> None:
    assert resolve_inside(tmp_path, target).path is None
    (tmp_path / "normal").write_text("data")
    (tmp_path / "alias").symlink_to(tmp_path / ".env")
    assert resolve_inside(tmp_path, "alias").path is None
    (tmp_path / "hard").hardlink_to(tmp_path / "normal")
    assert resolve_inside(tmp_path, "hard").path is None


# 功能：验证外部来源、失配服务端和过期上下文均被网关拒绝
# 设计：仅变更不可变上下文的一个字段，定位每种身份边界
@pytest.mark.parametrize(
    "changes",
    [
        {"trust_origin": "external"},
        {"server_id": "spoof"},
        {"deadline": 0},
        {"principal_id": ""},
        {"operation_id": ""},
    ],
)
def test_invalid_context(tmp_path: Path, changes: dict[str, object]) -> None:
    manager = PermissionManager(project_root=tmp_path)
    ctx = replace(context(manager), **changes)
    decision = SecurityGate().evaluate(
        ctx,
        ToolCallBlock("c", "read_file", {"path": "x"}),
        ReadFileTool(tmp_path),
        manager,
    )
    assert decision.action == PermissionDecision.DENY


# 功能：验证显式 ask 规则不能被 auto 分类器的 SAFE 覆盖
# 设计：注入可观测的假分类器，断言未调用且保持审批义务
async def test_ask_rule_not_classified(tmp_path: Path) -> None:
    manager = PermissionManager(
        project_root=tmp_path,
        mode="auto",
        timeout_s=0.001,
        rules=(Rule("global", "write_file", PermissionDecision.ASK),),
    )
    classifier = AsyncMock()
    manager.classifier = classifier
    registry = ToolRegistry()
    registry.register(WriteFileTool(tmp_path))
    result = await invoke_tool(
        registry,
        ToolCallBlock("c", "write_file", {"path": "x", "content": "x"}),
        EventBus(),
        "r",
        permission_manager=manager,
    )
    classifier.classify.assert_not_called()
    assert result.error_type == "permission_denied"


# 功能：验证无审批管理器时未知副作用工具不能执行
# 设计：使用真实写工具并检查文件不存在，覆盖省略 manager 的旧绕过入口
async def test_missing_manager_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    registry = ToolRegistry()
    registry.register(WriteFileTool(tmp_path))
    result = await invoke_tool(
        registry,
        ToolCallBlock("c", "write_file", {"path": "x", "content": "x"}),
        EventBus(),
        "r",
    )
    assert result.error_type == "permission_denied"
    assert not (tmp_path / "x").exists()


# 功能：验证项目 allow 不能覆盖上级 deny 或强制 ask
# 设计：使用分层规则的真实评估路径，确保优先级不受输入顺序影响
def test_project_cannot_escalate(tmp_path: Path) -> None:
    manager = PermissionManager(
        project_root=tmp_path,
        rules=(
            Rule("project", "write_file", PermissionDecision.ALLOW),
            Rule("global", "write_file", PermissionDecision.DENY),
        ),
    )
    assert manager.evaluate("write_file", {}) == PermissionDecision.DENY
