from __future__ import annotations

from typing import Any

import pytest

from claudedock.core.compact.microcompact import microcompact, recent_boundary, validate_messages


# 功能：验证多工具组与签名完整保留，仅清理旧可恢复结果
# 设计：最近轮次含 thinking 与两个调用，逐块比较确保协议数据未改动
def test_preserve_recent_tools_and_signatures() -> None:
    messages: list[dict[str, Any]] = []
    for turn in range(3):
        messages += [
            {"role": "user", "content": f"request {turn}"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "opaque", "signature": "sig"},
                {"type": "tool_use", "id": f"a{turn}", "name": "read_file", "input": {}},
                {"type": "tool_use", "id": f"b{turn}", "name": "read_file", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"a{turn}", "content": "x" * 1000 + "\n[tool-result-ref:ref]"},
                {"type": "tool_result", "tool_use_id": f"b{turn}", "content": "important unarchived evidence"},
            ]},
        ]
    candidate, count = microcompact(messages, 1)
    assert count == 2
    assert recent_boundary(messages, 1) == 6
    assert candidate[6:] == messages[6:]
    assert candidate[1] == messages[1]
    assert candidate[2]["content"][1] == messages[2]["content"][1]


# 功能：验证未完成工具组不能被提交或裁剪
# 设计：构造双调用单结果，直接触发完整性校验的危险边界
def test_unresolved_group_rejected() -> None:
    with pytest.raises(ValueError):
        validate_messages([
            {"role": "assistant", "content": [{"type": "tool_use", "id": "a"}, {"type": "tool_use", "id": "b"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a"}]},
        ])

# 功能：验证单次用户请求中的长工具链也能分组清理，而非等待新的人类消息
# 设计：构造三十步工具链，确保只保留最近四组且调用结果始终完整配对
async def test_single_request_long_tool_chain() -> None:
    messages: list[dict[str, Any]] = [{"role": "user", "content": "one long task"}]
    for index in range(30):
        messages.extend([
            {"role": "assistant", "content": [{"type": "tool_use", "id": str(index)}]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": str(index),
                "content": "x" * 1000 + "\n[tool-result-ref:id]",
            }]},
        ])
    candidate, pruned = microcompact(messages, 4)
    assert pruned == 26
    assert candidate[-8:] == messages[-8:]
    validate_messages(candidate)
