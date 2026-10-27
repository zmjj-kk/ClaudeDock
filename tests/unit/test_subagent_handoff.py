from __future__ import annotations

from pathlib import Path

from claudedock.core.subagent.handoff import ResultSummary, TaskHandoff


# 功能：验证模型交接数据无法自行扩大工具权限
# 设计：伪造工具白名单字段与可信程序白名单相冲突，最终只能保留程序值
def test_handoff_controls_are_program_owned() -> None:
    handoff = TaskHandoff.build(
        "goal", {"tools_allowed": ["bash"], "expected_output": "review"},
        ["do not modify files"], ["read_file"], "parent",
    )
    assert handoff.tools_allowed == ["read_file"]
    assert handoff.known_constraints == ["do not modify files"]


# 功能：验证助手完成声明不自动成为父任务可验证执行事实
# 设计：提供声称测试通过的文本但没有操作日志，检查结构化结果明确保留不确定性
def test_result_claim_requires_execution_evidence(tmp_path: Path) -> None:
    summary = ResultSummary.build("r", "success", "All tests passed", {}, [], tmp_path)
    assert summary.verified_actions == []
    assert summary.confidence == "assistant_claims_unverified"
    assert summary.open_issues

# 功能：验证父侧发现子任务结束后文件又被修改时不再信任原文件状态
# 设计：保存旧摘要后修改真实文件，父侧再次校验必须标记失效
async def test_parent_rechecks_changed_file(tmp_path: Path) -> None:
    import hashlib

    from claudedock.core.subagent.handoff import verify_result_files

    path = tmp_path / "file.py"
    path.write_text("old")
    summary = {"files_changed": [{"path": "file.py", "digest": hashlib.sha256(b"old").hexdigest()}]}
    path.write_text("new")
    verified = verify_result_files(summary, tmp_path)
    assert not verified["files_changed"][0]["verified_current"]
    assert verified["open_issues"]
