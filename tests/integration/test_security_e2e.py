from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.security.classifier import RiskClassifier
from kama_claude.core.security.decision_audit import SecurityAudit
from kama_claude.core.tools.builtin.bash import BashTool
from kama_claude.core.tools.builtin.write_file import WriteFileTool
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry


# 功能：验证 auto 普通编辑实际生效，敏感路径即使分类器 SAFE 也不执行
# 设计：真实执行写文件与审计，只替换独立 AI 提供者以避免任何计费
async def test_auto_edit_and_hard_denial(tmp_path: Path) -> None:
    calls = 0

    # 模拟独立分类器，验证宿主不会用它覆盖硬路径边界
    async def safe(system: str, payload: str, budget: int) -> str:
        nonlocal calls
        calls += 1
        return '{"verdict":"SAFE","risk":"low","reason":"ordinary bounded text edit"}'

    audit = SecurityAudit(tmp_path / "security.jsonl")
    manager = PermissionManager(
        project_root=tmp_path,
        mode="auto",
        classifier=RiskClassifier(safe, model_version="offline-test"),
        audit=audit,
    )
    registry = ToolRegistry()
    registry.register(WriteFileTool(tmp_path))
    for tool_id, path in [("ordinary", "note.txt"), ("protected", ".env")]:
        result = await invoke_tool(
            registry,
            ToolCallBlock(tool_id, "write_file", {"path": path, "content": "hello"}),
            EventBus(),
            "r",
            permission_manager=manager,
        )
        assert result.is_error == (path == ".env")
    assert (tmp_path / "note.txt").read_text() == "hello"
    assert not (tmp_path / ".env").exists() and calls == 1
    records = [json.loads(line) for line in audit.path.read_text().splitlines()]
    assert any(row["event"].get("reason") == "classifier_safe" for row in records)
    assert all("hello" not in json.dumps(row) for row in records)


# 功能：验证沙箱缺失与显式禁用均不能降级执行宿主 shell
# 设计：使用真实 BashTool 的错误通路并检查文件未生成，不依赖机器安装 bubblewrap
async def test_shell_cannot_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda command: None)
    for sandbox in (True, False):
        tool = BashTool(root=tmp_path, sandbox=sandbox)
        if sandbox:
            result = await tool.invoke({"command": "touch escaped"})
            assert result.is_error and "sandbox unavailable" in result.content.lower()
        else:
            registry = ToolRegistry()
            registry.register(tool)
            result = await invoke_tool(
                registry,
                ToolCallBlock("c", "bash", {"command": "touch escaped"}),
                EventBus(),
                "r",
                permission_manager=PermissionManager(project_root=tmp_path),
            )
            assert result.error_type == "permission_denied"
    assert not (tmp_path / "escaped").exists()


# 功能：验证生产调用图中仅统一网关可以调用 BaseTool.invoke
# 设计：扫描 AST 的真实调用表达式，防止子 Agent 或后台任务新增直接调用入口
def test_all_tool_execution_uses_gateway() -> None:
    root = Path(__file__).resolve().parents[2] / "src/kama_claude"
    direct = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "invoke":
                    direct.append(path.relative_to(root).as_posix())
    assert direct == ["core/tools/invocation.py"]


# 功能：验证沙箱工作区默认只读，并遮蔽嵌套密钥和 .git 工作树指针文件
# 设计：仅记录 bubblewrap 参数，断言挂载权限而不在缺失沙箱的机器上伪造执行成功
def test_shell_mount_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/" + command)
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/.env").write_text("secret")
    (tmp_path / ".git").write_text("gitdir: /outside")
    argv = BashTool(root=tmp_path)._argv("echo hi", 1)
    mount = argv.index(str(tmp_path))
    assert argv[mount - 1] == "--ro-bind"
    assert "/workspace/nested/.env" in argv and "/workspace/.git" in argv
    assert "--unshare-all" in argv and "--cap-drop" in argv
