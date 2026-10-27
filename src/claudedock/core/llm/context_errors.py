from __future__ import annotations

from typing import Any


class ContextWindowExceeded(RuntimeError):
    pass


# 仅识别明确容量错误，不把认证、配额与普通参数错误当作上下文溢出
def is_context_error(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    error = body.get("error", body)
    if not isinstance(error, dict):
        return False
    if error.get("type") in {"context_window_exceeded", "prompt_too_long"}:
        return True
    message = str(error.get("message", "")).lower()
    return error.get("type") == "invalid_request_error" and (
        message.startswith("prompt is too long:")
        or message.startswith("input length and max_tokens exceed context limit")
    )
