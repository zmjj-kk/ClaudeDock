from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from claudedock.core.mcp.client import McpClient


# 功能：call_tool 应将非 text 内容转为占位符，而非静默丢弃
# 设计：mock _call 返回含 text/image/resource/audio 的 content，断言占位符与文本并存
@pytest.mark.asyncio
async def test_call_tool_preserves_non_text_content() -> None:
    client = McpClient()
    client._call = AsyncMock(return_value={  # type: ignore[method-assign]
        "content": [
            {"type": "text", "text": "hello"},
            {"type": "image", "data": "abc"},
            {"type": "resource", "resource": {"text": "embedded"}},
            {"type": "audio", "data": "xyz"},
        ]
    })
    result = await client.call_tool("screenshot", {})
    assert "hello" in result
    assert "[image content]" in result
    assert "embedded" in result
    assert "[audio content]" in result


# 功能：read_timeout 应可通过构造函数配置，用于响应读取
# 设计：构造时传入 120.0，断言内部 _read_timeout 记录该值；默认值为 30.0
def test_read_timeout_configurable() -> None:
    assert McpClient(read_timeout=120.0)._read_timeout == 120.0
    assert McpClient()._read_timeout == 30.0


# 功能：close 应清理引用且可幂等重复调用
# 设计：构造后连续 close 两次，断言不抛异常且内部引用已置空
@pytest.mark.asyncio
async def test_close_idempotent() -> None:
    client = McpClient()
    await client.close()
    await client.close()
    assert client._transport == ""
    assert client._proc is None
    assert client._reader is None
    assert client._tcp_writer is None
