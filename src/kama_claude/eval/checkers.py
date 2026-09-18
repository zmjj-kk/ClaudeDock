from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from kama_claude.core.tools.base import BaseTool, ToolResult


# 断言 ToolResult 的 is_error / error_type / content 满足给定预期
def assert_tool_result(
    result: ToolResult,
    *,
    is_error: bool = False,
    error_type: str | None = None,
    content_contains: str | None = None,
) -> None:
    assert result.is_error is is_error, f"is_error 期望 {is_error}，实际 {result.is_error}"
    if error_type is not None:
        assert result.error_type == error_type, (
            f"error_type 期望 {error_type!r}，实际 {result.error_type!r}"
        )
    if content_contains is not None:
        assert content_contains in result.content, (
            f"content 未包含 {content_contains!r}，实际 {result.content!r}"
        )


# 断言参数通过工具 params_model 校验，不抛 ValidationError
def assert_schema_valid(tool: BaseTool, params: dict[str, Any]) -> None:
    assert tool.params_model is not None, f"{tool.name} 未定义 params_model，无法做 schema 校验"
    tool.params_model.model_validate(params)


# 断言参数被工具 params_model 拒绝，抛 ValidationError
def assert_schema_invalid(tool: BaseTool, params: dict[str, Any]) -> None:
    assert tool.params_model is not None, f"{tool.name} 未定义 params_model，无法做 schema 校验"
    try:
        tool.params_model.model_validate(params)
    except ValidationError:
        return
    raise AssertionError(f"{tool.name} 应拒绝参数 {params!r}，但校验通过")
