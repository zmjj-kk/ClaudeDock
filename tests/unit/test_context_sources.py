from __future__ import annotations

from pathlib import Path

from kama_claude.core.context_sources.loader import InstructionLoader


# 功能：验证来源哈希、内容去重与目录规则只在目标目录加载
# 设计：同内容顶层文件与两个子目录并列，确认没有递归读取无关规则
def test_source_snapshot_scope_and_hash(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("project rules")
    (tmp_path / "AGENTS.md").write_text("project rules")
    (tmp_path / "target").mkdir()
    (tmp_path / "other").mkdir()
    (tmp_path / "target/AGENTS.md").write_text("target rules")
    (tmp_path / "other/AGENTS.md").write_text("not relevant")
    snapshot = InstructionLoader(tmp_path).load(tmp_path / "target")
    assert len(snapshot.sources) == 2
    assert "not relevant" not in snapshot.render()
    assert all(source.trust_level == "external" for source in snapshot.sources)
    original_hash = snapshot.sources[0].source_hash
    (tmp_path / "CLAUDE.md").write_text("new rules")
    assert InstructionLoader(tmp_path).load().sources[0].source_hash != original_hash


# 功能：验证导入环、越界链接和过大规则文件不会被静默加载
# 设计：用真实临时文件和软链接执行负向路径，保持权限边界可测
def test_import_cycle_escape_and_limits(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    (workspace / "CLAUDE.md").write_text("@import rules.md")
    (workspace / "rules.md").write_text("@import CLAUDE.md")
    (workspace / "AGENTS.md").symlink_to(outside)
    snapshot = InstructionLoader(workspace).load()
    assert any("cycle" in item for item in snapshot.diagnostics)
    assert any("escapes" in item for item in snapshot.diagnostics)
    assert "secret" not in snapshot.render()
    assert InstructionLoader(workspace, max_file_bytes=1).load().diagnostics

# 功能：验证规则文件不能通过导入环境文件读取密钥，正文里的密钥也会脱敏
# 设计：同时构造非法后缀导入与 Markdown 正文密钥，覆盖路径与内容两道边界
async def test_sources_do_not_import_secrets(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("@import .env\napi_key=sk-1234567890abcdef")
    (tmp_path / ".env").write_text("secret=private_environment_value")
    snapshot = InstructionLoader(tmp_path).load()
    assert "private_environment_value" not in snapshot.render()
    assert "sk-1234567890abcdef" not in snapshot.render()
    assert any("markdown or text" in message for message in snapshot.diagnostics)
