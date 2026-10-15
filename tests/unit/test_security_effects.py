from __future__ import annotations

import time
from pathlib import Path

import pytest

from kama_claude.core.security.context import digest
from kama_claude.core.security.decision_audit import SecurityAudit
from kama_claude.core.security.effects import EffectExecutor, PreparedEffect


class Backend:
    # 使用模拟服务端账本验证客户端不会盲目重发已进入执行窗口的操作
    def __init__(self) -> None:
        self.state = "prepared"
        self.calls = 0

    # 返回可信后端维护的操作状态，不依赖 Agent 的本地 journal
    async def status(self, principal_id: str, operation_id: str) -> str:
        return self.state

    # 模拟副作用已经发出但响应超时，后端将结果标记为未知
    async def execute(
        self,
        effect: PreparedEffect,
        arguments: dict[str, object],
        approval: str,
    ) -> str:
        self.calls += 1
        self.state = "unknown"
        raise TimeoutError


# 功能：验证超时后查询状态，同 operation_id 不再次写入，参数变化立即拒绝
# 设计：模拟副作用发出后超时的真实危险窗口，检查后端执行计数严格为一
async def test_business_unknown_outcome_not_retried() -> None:
    backend = Backend()
    executor = EffectExecutor(backend)
    arguments = {"order": 1}
    effect = PreparedEffect("op", "p", digest(arguments), "impact", time.time() + 60, "signed")
    assert await executor.execute(effect, arguments, "p", "approval") == "unknown"
    assert await executor.execute(effect, arguments, "p", "approval") == "unknown"
    assert backend.calls == 1
    with pytest.raises(PermissionError):
        await executor.execute(effect, {"order": 2}, "p", "approval")


# 功能：验证安全审计脱敏、哈希链、轮转与文件权限
# 设计：使用很小的轮转上限与真实文件，检查跨文件链尾关联而非只断言 log 被调用
def test_audit_rotation_and_chain(tmp_path: Path) -> None:
    audit = SecurityAudit(tmp_path / "audit.jsonl", max_bytes=1)
    audit.record({"type": "security.decision", "api_key": "private"})
    audit.record({"type": "security.execution", "operation_id": "r:c"})
    import json

    previous = json.loads((tmp_path / "audit.jsonl.1").read_text())
    current = json.loads(audit.path.read_text())
    assert current["previous_hash"] == previous["hash"]
    assert "private" not in (tmp_path / "audit.jsonl.1").read_text()
    assert audit.path.stat().st_mode & 0o077 == 0
    current["event"]["operation_id"] = "tampered"
    audit.path.write_text(json.dumps(current) + "\n")
    with pytest.raises(PermissionError):
        audit.record({"type": "next"})
