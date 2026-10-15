from __future__ import annotations

from pathlib import Path

import pytest

from kama_claude.core.tools.builtin.bash import BashTool
from kama_claude.core.tools.builtin.list_dir import ListDirTool
from kama_claude.core.tools.builtin.write_file import WriteFileTool

# ── bash ──────────────────────────────────────────────────────────────────────


# 功能：验证成功命令的 stdout 出现在 ToolResult.content 中，is_error 为 False
# 设计：用 echo 命令避免外部依赖，直接比较输出内容，无需 mock
@pytest.mark.asyncio
async def test_bash_success_stdout() -> None:
    result = await BashTool(sandbox=False).invoke({"command": "echo hello"})
    assert not result.is_error
    assert "hello" in result.content


# 功能：验证非零退出码时 is_error=True 且 content 包含退出码标注
# 设计：`exit 2` 是最简单的非零退出；不依赖任何外部命令行为
@pytest.mark.asyncio
async def test_bash_nonzero_exit_is_error() -> None:
    result = await BashTool(sandbox=False).invoke({"command": "exit 2"})
    assert result.is_error
    assert "[exit 2]" in result.content


# 功能：验证命令超时后 is_error=True，error_type 为 "timeout"
# 设计：timeout=1s 搭配 sleep 2 必然超时；验证 error_type 而非 content，避免超时消息格式耦合
@pytest.mark.asyncio
async def test_bash_timeout() -> None:
    result = await BashTool(sandbox=False).invoke({"command": "sleep 5", "timeout": 1})
    assert result.is_error
    assert result.error_type == "timeout"


# 功能：验证 stderr 被合并到 stdout 输出中
# 设计：只写 stderr 的命令（>&2 echo），输出应该出现在合并后的 content 里
@pytest.mark.asyncio
async def test_bash_stderr_merged() -> None:
    result = await BashTool(sandbox=False).invoke({"command": "echo err >&2"})
    assert not result.is_error
    assert "err" in result.content


# ── write_file ────────────────────────────────────────────────────────────────


# 功能：验证 write_file 写入文件后内容可以被读取，返回字节数
# 设计：注入 root=tmp_path 后按相对路径写入，断言文件存在且内容一致；用 tmp_path fixture 自动清理
@pytest.mark.asyncio
async def test_write_file_creates_and_returns_size(tmp_path: Path) -> None:
    result = await WriteFileTool(root=tmp_path).invoke(
        {"path": "out.txt", "content": "hello world"}
    )
    assert not result.is_error
    assert "11" in result.content  # "hello world" = 11 bytes
    assert (tmp_path / "out.txt").read_text() == "hello world"


# 功能：验证 write_file 自动创建不存在的父目录
# 设计：注入 root=tmp_path 后按相对路径写入，确认父目录结构被创建
@pytest.mark.asyncio
async def test_write_file_creates_parent_dirs(tmp_path: Path) -> None:
    result = await WriteFileTool(root=tmp_path).invoke({"path": "a/b/file.txt", "content": "x"})
    assert not result.is_error
    assert (tmp_path / "a" / "b" / "file.txt").exists()


# 功能：验证 write_file 拒绝包含 .. 的路径并返回 permission_denied
# 设计：.. 路径遍历与 read_file 遵循相同规则；安全边界改返回结构化 ToolResult 而非抛异常
@pytest.mark.asyncio
async def test_write_file_rejects_traversal(tmp_path: Path) -> None:
    result = await WriteFileTool(root=tmp_path).invoke({"path": "../secret.txt", "content": "x"})
    assert result.is_error
    assert result.error_type == "permission_denied"


# ── list_dir ──────────────────────────────────────────────────────────────────


# 功能：验证 list_dir 输出包含目录中的文件名
# 设计：注入 root=tmp_path 后列出 "."，断言文件名出现在 content 中；不约束格式细节
@pytest.mark.asyncio
async def test_list_dir_shows_files(tmp_path: Path) -> None:
    (tmp_path / "foo.py").write_text("x")
    (tmp_path / "bar.md").write_text("y")
    result = await ListDirTool(root=tmp_path).invoke({"path": "."})
    assert not result.is_error
    assert "foo.py" in result.content
    assert "bar.md" in result.content


# 功能：验证 list_dir 按 max_depth 限制递归深度（depth=1 时不展示孙级目录内容）
# 设计：创建 parent/child/grandchild 三层，depth=1 时 grandchild 不应出现在输出中
@pytest.mark.asyncio
async def test_list_dir_respects_max_depth(tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()
    grandchild = child / "grandchild"
    grandchild.mkdir()
    (grandchild / "deep.txt").write_text("x")

    result = await ListDirTool(root=tmp_path).invoke({"path": ".", "max_depth": 1})
    assert not result.is_error
    assert "child" in result.content
    assert "deep.txt" not in result.content


# 功能：验证对不存在的路径 list_dir 抛出 FileNotFoundError
# 设计：注入 root=tmp_path 后传入不存在的相对路径，预期抛出标准异常（invocation.py 捕获后返回 error ToolResult）
@pytest.mark.asyncio
async def test_list_dir_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await ListDirTool(root=tmp_path).invoke({"path": "no/such/dir"})


# 功能：验证 list_dir 拒绝包含 .. 的路径并返回 permission_denied
# 设计：与 read_file 和 write_file 保持一致的安全规则；越界改返回结构化 ToolResult
@pytest.mark.asyncio
async def test_list_dir_rejects_traversal(tmp_path: Path) -> None:
    result = await ListDirTool(root=tmp_path).invoke({"path": "../"})
    assert result.is_error
    assert result.error_type == "permission_denied"
