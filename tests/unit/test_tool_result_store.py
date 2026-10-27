from __future__ import annotations

import json
from pathlib import Path

import pytest

from claudedock.core.compact.tool_result_store import ResultBudget, ToolResultStore
from claudedock.core.tools.base import ToolResult


# 功能：验证两兆输出只进入有限预览，原文可校验分页读取
# 设计：检查真实落盘权限、摘要与分页内容，而非只断言返回字符串截断
def test_large_result_externalized(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path / "private")
    text = "中" * 700000
    result = store.bound(ToolResult(text), "run", "call", 131072)
    assert len(result.content) < 4500
    result_id = result.content.split("[tool-result-ref:")[1].split(";")[0]
    page = json.loads(store.read(result_id, 0, 300))
    assert page["text"] == "中" * 100
    assert page["next_offset"] == 300
    assert (store.root / result_id).stat().st_mode & 0o777 == 0o600


# 功能：验证会话隔离、路径逃逸与内容损坏不能读取
# 设计：分别使用另一存储实例、恶意标识及修改原文件进行负向验证
def test_ownership_and_digest(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path / "a", max_bytes=1)
    result = store.bound(ToolResult("evidence"), "run", "call", 1000)
    result_id = result.content.split("[tool-result-ref:")[1].split(";")[0]
    other = ToolResultStore(tmp_path / "b")
    with pytest.raises(OSError):
        other.read(result_id)
    with pytest.raises(ValueError):
        store.read("../secret")
    (store.root / result_id).write_text("tampered")
    with pytest.raises(ValueError, match="checksum"):
        store.read(result_id)


# 功能：验证整轮额度与磁盘配额限制且保留已执行状态
# 设计：第二个小结果合计超限，再用极小配额模拟外置失败
def test_round_limit_and_storage_failure(tmp_path: Path) -> None:
    store = ToolResultStore(tmp_path / "a", max_bytes=1000)
    budget = ResultBudget(store, "run", 100)
    assert budget.bound(ToolResult("a" * 80), "a").content == "a" * 80
    bounded = budget.bound(ToolResult("b" * 80), "b")
    assert len(bounded.content.encode("utf-8")) <= 20
    assert bounded.content.startswith("Output omitted:")
    assert budget.remaining == 0
    tiny = ToolResultStore(tmp_path / "b", max_bytes=1, quota_bytes=1)
    error = tiny.bound(ToolResult("already executed"), "run", "call", 10)
    assert error.error_type == "output_storage_error"
    assert "do not repeat" in error.content
