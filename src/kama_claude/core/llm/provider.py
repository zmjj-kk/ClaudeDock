from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
from datetime import UTC, datetime
from typing import Any

import anthropic
import httpx

from kama_claude.core.bus.events import (
    ContextCacheUsageEvent,
    LlmModelSelectedEvent,
    LlmTokenEvent,
    LlmUsageEvent,
)
from kama_claude.core.compact.prompt import PromptText
from kama_claude.core.compact.token_budget import ModelCapacity
from kama_claude.core.context import current_budget
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.context_errors import ContextWindowExceeded, is_context_error
from kama_claude.core.llm.governed import BudgetExceeded
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock, UsageStats

_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # Claude 5.x 系列 1M 上下文；4.x 系列 200K；Haiku 4.5 200K
    "claude-opus-5-5": 1_000_000,
    "claude-sonnet-5-5": 1_000_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
    "claude-opus-4-7": 200_000,
}

_MAX_STREAM_RETRIES = 3
_RETRY_BACKOFF_S = (1.0, 2.0, 4.0)

log = logging.getLogger(__name__)


# 返回已配置模型的上下文窗口；未知模型显式保留未知状态
def _context_window(model: str) -> int | None:
    return _MODEL_CONTEXT_WINDOWS.get(model)


_SYSTEM_PROMPT = (
    "You are a helpful AI assistant. "
    "Use the available tools to complete the user's goal. "
    "When the goal is fully achieved, respond with a final answer and do not call any more tools."
)


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class AnthropicProvider:
    # 初始化 Anthropic 客户端；client 可在测试时注入以跳过 API key 检查
    def __init__(
        self, model: str, client: Any = None, *, context_window: int | None = None,
    ) -> None:
        if client is None:
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise SystemExit("ANTHROPIC_API_KEY not set")
            self._client: Any = anthropic.AsyncAnthropic(
                api_key=api_key, max_retries=0, timeout=60.0
            )
        else:
            self._client = client
        self._model = model
        self.context_capacity = ModelCapacity(context_window or _context_window(model))

    # 关闭复用的模型 HTTP 客户端
    async def aclose(self) -> None:
        await self._client.close()

    # 流式调用 Anthropic API，逐 token 发布事件并返回 LlmResponse；网络中断时自动重试
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        await bus.publish(
            LlmModelSelectedEvent(run_id=run_id, model=self._model, strategy="static", ts=_now())
        )

        system_blocks: list[dict[str, object]] = [
            {
                "type": "text",
                "text": system or _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            },
        ]

        stable_text = str(system or _SYSTEM_PROMPT)
        if isinstance(system, PromptText):
            stable_text = system.stable_prefix
            system_blocks = [{
                "type": "text", "text": stable_text,
                "cache_control": {"type": "ephemeral"},
            }]
            if system.dynamic_suffix:
                system_blocks.append({"type": "text", "text": system.dynamic_suffix})
        stable_hash = hashlib.sha256(stable_text.encode("utf-8")).hexdigest()
        cache_fallback = False
        tools: list[dict[str, object]] = list(tool_schemas)
        if tools:
            last = dict(tools[-1])
            last["cache_control"] = {"type": "ephemeral"}
            tools = tools[:-1] + [last]

        kwargs: dict[str, object] = {
            "model": self._model,
            "max_tokens": self.context_capacity.max_output,
            "system": system_blocks,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        text_parts: list[str] = []
        final_message: Any = None

        for attempt in range(1, _MAX_STREAM_RETRIES + 1):
            text_parts = []
            if attempt > 1:
                budget = current_budget.get()
                if budget is not None:
                    if budget.model_calls >= budget.max_model_calls:
                        raise BudgetExceeded("model_call_budget_exhausted")
                    budget.model_calls += 1
            try:
                async with self._client.messages.stream(**kwargs) as stream:
                    async for text in stream.text_stream:
                        # Only publish token events on the first attempt to avoid TUI duplicates
                        if attempt == 1:
                            await bus.publish(LlmTokenEvent(run_id=run_id, token=text, ts=_now()))
                        text_parts.append(text)
                    final_message = await stream.get_final_message()
                break  # success
            except anthropic.BadRequestError as exc:
                if is_context_error(exc.body):
                    raise ContextWindowExceeded("context_window_exceeded") from exc
                body = exc.body if isinstance(exc.body, dict) else {}
                error = body.get("error", body)
                message = (
                    str(error.get("message", "")).casefold() if isinstance(error, dict) else ""
                )
                if (
                    not cache_fallback and attempt < _MAX_STREAM_RETRIES
                    and ("cache_control" in message or "prompt caching" in message)
                    and any(word in message for word in ("unsupported", "unknown", "not supported"))
                ):
                    cache_fallback = True
                    kwargs["system"] = [
                        {key: value for key, value in block.items() if key != "cache_control"}
                        for block in system_blocks
                    ]
                    if tools:
                        kwargs["tools"] = [
                            {key: value for key, value in tool.items() if key != "cache_control"}
                            for tool in tools
                        ]
                    continue
                raise
            except (
                httpx.RemoteProtocolError,
                httpx.ReadError,
                httpx.ConnectError,
                anthropic.RateLimitError,
                anthropic.InternalServerError,
                anthropic.APIConnectionError,
            ) as exc:
                if text_parts:
                    raise RuntimeError("LLM stream interrupted after partial output") from exc
                if attempt == _MAX_STREAM_RETRIES:
                    log.error(
                        "stream failed after %d attempts run_id=%s step=%d: %s",
                        _MAX_STREAM_RETRIES,
                        run_id,
                        step,
                        exc,
                    )
                    raise
                delay = _RETRY_BACKOFF_S[attempt - 1] + random.uniform(0, 0.25)
                log.warning(
                    "stream dropped (attempt %d/%d) run_id=%s step=%d: %s — retrying in %.0fs",
                    attempt,
                    _MAX_STREAM_RETRIES,
                    run_id,
                    step,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

        assert final_message is not None

        usage = final_message.usage
        cache_read: int = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_create: int = getattr(usage, "cache_creation_input_tokens", 0) or 0
        window = self.context_capacity.window
        context_pct = (usage.input_tokens + cache_read + cache_create) / window if window else 0.0

        await bus.publish(ContextCacheUsageEvent(
            run_id=run_id, stable_prefix_hash=stable_hash,
            cache_read=cache_read, cache_create=cache_create, ts=_now(),
        ))
        await bus.publish(
            LlmUsageEvent(
                run_id=run_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_create,
                context_pct=context_pct,
                context_window=window,
                ts=_now(),
            )
        )

        tool_calls: list[ToolCallBlock] = []
        thinking_blocks: list[dict[str, object]] = []
        for block in final_message.content:
            if block.type == "tool_use":
                tool_calls.append(
                    ToolCallBlock(id=block.id, name=block.name, input=dict(block.input))
                )
            elif block.type == "thinking":
                # thinking blocks must be passed back verbatim in subsequent requests
                thinking_blocks.append(
                    {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
                )

        return LlmResponse(
            stop_reason=final_message.stop_reason or "end_turn",
            tool_calls=tool_calls,
            text="".join(text_parts),
            thinking_blocks=thinking_blocks,
            usage=UsageStats(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_create,
                context_pct=context_pct,
            ),
        )
