from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from claudedock.core.permissions.manager import PermissionManager
from claudedock.core.security.approval import ApprovalScope, ApprovalTokens


# 功能：验证审批 token 对所有身份、参数、影响与策略字段精确绑定且一次性消费
# 设计：逐字段变更同一审批范围，确保不存在只校验部分字段的漏洞
@pytest.mark.parametrize(
    "field",
    [
        "principal_id",
        "session_id",
        "operation_id",
        "server_id",
        "tool_identity",
        "argument_hash",
        "effect_summary_hash",
        "policy_revision",
    ],
)
def test_scope_token_replay(field: str) -> None:
    scope = ApprovalScope("p", "s", "op", "server", "tool", "args", "effect", "v1")
    tokens = ApprovalTokens()
    token = tokens.issue(scope)
    assert not tokens.consume(token, replace(scope, **{field: "different"}))
    assert not tokens.consume(token, scope)
    token = tokens.issue(scope)
    assert tokens.consume(token, scope)
    assert not tokens.consume(token, scope)


# 功能：验证真实审批请求拒绝旧 token，策略变更后已批准请求仍不能生效
# 设计：在事件回调内变更主体，模拟人工审批窗口中的安全配置变化
async def test_manager_scope_change(tmp_path: Path) -> None:
    manager = PermissionManager(project_root=tmp_path)

    # 审批事件只允许正确 token，批准后主体变化撤销整个请求
    async def respond(event: dict[str, object]) -> None:
        with pytest.raises(PermissionError):
            manager.respond("c", "allow_once", approval_token="replayed")
        manager.respond("c", "allow_once", approval_token=str(event["approval_token"]))
        manager.principal_id = "other"

    allowed, reason = await manager.check_and_wait(
        "c",
        "write_file",
        {"path": "x", "content": "x"},
        "s",
        respond,
        approval_scope="exact-scope",
    )
    assert not allowed and reason == "policy_changed"


# 功能：验证审批超时后旧 token 不能解决相同工具 ID 的新请求
# 设计：重用调用 ID 并延迟回放首个 token，覆盖跨请求重放窗口
async def test_late_token_cannot_approve_new_request() -> None:
    manager = PermissionManager(timeout_s=0.001)
    events: list[dict[str, object]] = []

    # 保存审批 token，用于在下一次请求中模拟迟到响应
    async def capture(event: dict[str, object]) -> None:
        events.append(event)
        if len(events) > 1:
            with pytest.raises(PermissionError):
                manager.respond("c", "allow_once", approval_token=str(events[0]["approval_token"]))

    for _ in range(2):
        allowed, _ = await manager.check_and_wait(
            "c",
            "bash",
            {"command": "echo hi"},
            "s",
            capture,
            approval_scope="scope",
        )
        assert not allowed
    assert events[0]["approval_token"] != events[1]["approval_token"]
