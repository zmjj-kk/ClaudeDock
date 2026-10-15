from __future__ import annotations

from unittest.mock import AsyncMock

from kama_claude.tui.app import KamaTuiApp


# 功能：验证上下文查看通过真实 TUI 调用认证 IPC，并渲染笔记与上下文事件
# 设计：仅替换连接客户端，使用 Textual 运行环境检查新视图未退化为 CLI 输出
async def test_context_and_notes_view_in_tui(monkeypatch) -> None:
    app = KamaTuiApp("localhost", 1)
    monkeypatch.setattr(app, "_socket_loop", AsyncMock())
    client = AsyncMock()
    client.send_command.return_value = {
        "notes": "unverified notes [red] remain literal",
        "sources": [{"scope": "project", "source_path": "AGENTS.md", "source_hash": "hash"}],
        "projection": {"revision": 2},
    }
    app._client = client
    app._session_id = "s"
    async with app.run_test() as pilot:
        await app._do_context()
        client.send_command.assert_awaited_once_with("session.get_context", {"session_id": "s"})
        app._handle_event({
            "type": "context.memory_updated", "run_id": "r", "revision": 2, "source_count": 4,
        })
        app._handle_event({
            "type": "context.sources_loaded", "run_id": "r", "sources": [], "diagnostics": [],
        })
        await pilot.pause()
        assert len(app.query(".log-line")) >= 3
