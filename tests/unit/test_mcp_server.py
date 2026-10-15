from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from kama_claude.core.config import McpServerConfig
from kama_claude.core.mcp.client import McpClient
from kama_claude.core.mcp.server import McpServerManager


# 功能：start_all 遇重复 server name 应跳过，避免覆盖已连接的同名 client
# 设计：两个同名 config，断言 _connect 仅被调用一次
@pytest.mark.asyncio
async def test_start_all_skips_duplicate_name() -> None:
    mgr = McpServerManager()
    client = AsyncMock(spec=McpClient)
    client.list_tools = AsyncMock(return_value=[])
    mgr._connect = AsyncMock(return_value=client)  # type: ignore[method-assign]

    await mgr.start_all([McpServerConfig(name="dup"), McpServerConfig(name="dup")])
    assert mgr._connect.await_count == 1


# 功能：list_tools 失败时应关闭已建立的 client，避免泄漏 stdio 子进程
# 设计：_connect 返回一个 list_tools 抛错的 client，断言其 close 被调用
@pytest.mark.asyncio
async def test_start_all_closes_client_on_list_tools_failure() -> None:
    mgr = McpServerManager()
    client = AsyncMock(spec=McpClient)
    client.list_tools = AsyncMock(side_effect=RuntimeError("boom"))
    mgr._connect = AsyncMock(return_value=client)  # type: ignore[method-assign]

    await mgr.start_all([McpServerConfig(name="bad")])
    client.close.assert_awaited_once()


# 功能：_connect 应按 config.read_timeout 构造 McpClient
# 设计：monkeypatch server 模块的 McpClient 为记录参数的桩，断言 read_timeout 透传
@pytest.mark.asyncio
async def test_connect_passes_read_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, float] = {}

    class _Stub:
        def __init__(self, read_timeout: float = 30.0) -> None:
            captured["read_timeout"] = read_timeout

        async def connect_stdio(self, *args: object, **kwargs: object) -> None:
            return None

    monkeypatch.setattr("kama_claude.core.mcp.server.McpClient", _Stub)
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/" + command)
    mgr = McpServerManager()
    await mgr._connect(McpServerConfig(name="x", command="echo", read_timeout=90.0))
    assert captured["read_timeout"] == 90.0
