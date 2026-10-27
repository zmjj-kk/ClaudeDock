from __future__ import annotations

from copy import deepcopy
from typing import Any


# 验证完整工具调用块配对，禁止跨调用组删除 thinking 或结果
def validate_messages(messages: list[dict[str, Any]]) -> None:
    pending: set[str] = set()
    seen: set[str] = set()
    for message in messages:
        blocks = message.get("content", [])
        if not isinstance(blocks, list):
            if pending:
                raise ValueError("unresolved tool calls")
            continue
        calls = [b for b in blocks if b.get("type") == "tool_use"]
        results = [b for b in blocks if b.get("type") == "tool_result"]
        if calls:
            if message["role"] != "assistant" or pending:
                raise ValueError("invalid tool call group")
            ids = [str(b.get("id", "")) for b in calls]
            if not all(ids) or len(set(ids)) != len(ids) or seen.intersection(ids):
                raise ValueError("duplicate or empty tool id")
            pending = set(ids)
            seen.update(ids)
        elif pending:
            ids = [str(b.get("tool_use_id", "")) for b in results]
            if message["role"] != "user" or len(ids) != len(set(ids)) or set(ids) != pending:
                raise ValueError("incomplete tool result group")
            pending.clear()
        elif results:
            raise ValueError("orphan tool result")
    if pending:
        raise ValueError("unresolved tool calls")


# 以完整助手响应与工具结果为边界，支持单次用户请求中的长工具链
def recent_boundary(messages: list[dict[str, Any]], turns: int) -> int:
    validate_messages(messages)
    if turns <= 0:
        return len(messages)
    starts = [0] if messages else []
    for index, message in enumerate(messages):
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        closes_group = (
            message["role"] == "assistant"
            and not any(block.get("type") == "tool_use" for block in blocks)
        ) or (
            message["role"] == "user"
            and any(block.get("type") == "tool_result" for block in blocks)
        )
        if closes_group and index + 1 < len(messages):
            starts.append(index + 1)
    return starts[-turns] if len(starts) > turns else 0


# 只缩短已有可恢复引用的旧结果，保护近期与未外置证据
def microcompact(
    messages: list[dict[str, Any]], recent_turns: int
) -> tuple[list[dict[str, Any]], int]:
    boundary = recent_boundary(messages, recent_turns)
    candidate = deepcopy(messages)
    pruned = 0
    for message in candidate[:boundary]:
        blocks = message.get("content")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            content = block.get("content", "")
            if block.get("type") != "tool_result" or not isinstance(content, str):
                continue
            marker = "\n[tool-result-ref:"
            if marker in content:
                replacement = (
                    "[Older observation; retrieve evidence when needed]"
                    + marker + content.split(marker, 1)[1]
                )
                if len(replacement) < len(content):
                    block["content"] = replacement
                    pruned += 1
    validate_messages(candidate)
    return candidate, pruned
