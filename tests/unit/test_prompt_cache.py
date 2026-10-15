from __future__ import annotations

from kama_claude.core.compact.prompt import PromptText
from kama_claude.core.context import ExecutionContext


# 功能：验证目标与会话笔记变化不会改变稳定缓存前缀
# 设计：构造相同策略但不同动态任务的两个上下文，同时保持原字符串接口兼容
def test_dynamic_goal_does_not_change_stable_prefix() -> None:
    first = ExecutionContext("a", "first task", 5, prompt_cache_enabled=True)
    second = ExecutionContext("b", "second task", 5, prompt_cache_enabled=True)
    second.session_notes = "new progress"
    first_prompt = first.system_prompt("trusted policy")
    second_prompt = second.system_prompt("trusted policy")
    assert isinstance(first_prompt, PromptText) and isinstance(second_prompt, PromptText)
    assert first_prompt.stable_prefix == second_prompt.stable_prefix
    assert first_prompt.dynamic_suffix == second_prompt.dynamic_suffix == ""
    assert first.model_messages() != second.model_messages()
    plain = ExecutionContext("a", "first task", 5).system_prompt("trusted policy")
    assert str(first_prompt) == plain

# 功能：验证不支持缓存语法的代理仅移除缓存标记后有限重试
# 设计：第一次返回明确缓存参数错误，第二次使用真实 Provider 请求构建路径完成响应
async def test_cache_unsupported_falls_back_without_markers() -> None:
    from unittest.mock import AsyncMock, MagicMock

    import anthropic
    import httpx

    from kama_claude.core.events.bus import EventBus
    from kama_claude.core.llm.provider import AnthropicProvider

    final = MagicMock()
    final.stop_reason = "end_turn"
    final.content = []
    final.usage.input_tokens = 10
    final.usage.output_tokens = 1
    final.usage.cache_read_input_tokens = 0
    final.usage.cache_creation_input_tokens = 0
    stream = MagicMock()
    stream.__aenter__ = AsyncMock(return_value=stream)
    stream.__aexit__ = AsyncMock(return_value=False)
    stream.get_final_message = AsyncMock(return_value=final)

    # 提供有限异步文本流，避免调用真实 SDK 网络传输
    async def text_stream():
        yield "ok"

    stream.text_stream = text_stream()
    failure = anthropic.BadRequestError(
        "unsupported cache_control",
        response=httpx.Response(400, request=httpx.Request("POST", "https://example.invalid")),
        body={"error": {"type": "invalid_request_error", "message": "unsupported cache_control"}},
    )
    client = MagicMock()
    client.messages.stream.side_effect = [failure, stream]
    provider = AnthropicProvider("test-model", client=client)
    response = await provider.chat(
        messages=[{"role": "user", "content": "hello"}],
        tool_schemas=[{"name": "example", "input_schema": {"type": "object"}}],
        bus=EventBus(), run_id="r", system=PromptText("policy", "dynamic goal"),
    )
    assert response.text == "ok" and client.messages.stream.call_count == 2
    first, second = client.messages.stream.call_args_list
    assert "cache_control" in first.kwargs["system"][0]
    assert all("cache_control" not in block for block in second.kwargs["system"])
    assert all("cache_control" not in tool for tool in second.kwargs["tools"])
