from __future__ import annotations

import importlib.metadata
import os
import subprocess
import sys

import pytest

from claudedock import __version__
from claudedock.core.tools.paths import resolve_inside
from claudedock.tui.app import ClaudeDockTuiApp


# 功能：验证安装元数据中的公开命令全部指向新 Python 包
# 设计：检查实际安装的发行包，捕获源码重命名成功但安装入口仍过期的问题
def test_distribution_entry_points() -> None:
    distribution = importlib.metadata.distribution("claudedock")
    entries = {
        entry.name: entry.value
        for entry in distribution.entry_points
        if entry.group == "console_scripts"
    }
    assert entries == {
        "claudedock": "claudedock.cli.main:main",
        "claudedock-core": "claudedock.core.app:run",
        "claudedock-tui": "claudedock.tui.__main__:main",
    }


@pytest.mark.parametrize("option", ["--help", "--version"])
# 功能：验证真实 CLI 模块能够以新名称输出帮助与版本
# 设计：使用子进程覆盖实际模块入口，不调用模型 API 或依赖运行中的守护进程
def test_cli_branding(option: str) -> None:
    process = subprocess.run(
        [sys.executable, "-m", "claudedock.cli", option],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=True,
    )
    output = process.stdout + process.stderr
    if option == "--version":
        assert output.strip() == __version__
    else:
        assert "claudedock" in output.lower()


# 功能：验证 TUI 窗口使用统一品牌名称
# 设计：直接检查主应用标题，现有 TUI 集成测试继续覆盖交互与事件渲染
def test_tui_title() -> None:
    assert ClaudeDockTuiApp.TITLE == "ClaudeDock"


@pytest.mark.parametrize("directory", [".claudedock", ".kama"])
# 功能：验证品牌迁移后新旧私有目录均保持敏感路径保护
# 设计：覆盖旧配置仍留在其他工作区的情况，避免改名意外解除凭证目录拒绝规则
def test_private_state_paths_stay_protected(tmp_path, directory: str) -> None:
    result = resolve_inside(tmp_path, f"{directory}/token.txt")
    assert result.path is None
