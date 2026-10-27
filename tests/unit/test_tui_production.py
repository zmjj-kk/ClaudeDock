from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from claudedock.core.transport.socket_client import IpcError
from claudedock.tui.app import ClaudeDockTuiApp, PermissionBlock, PermissionSelect


class Client:
    # 保存界面请求与可以取消的模拟运行
    def __init__(self, app):
        self.app = app
        self.calls = []
        self.started = asyncio.Event()
        self.done = None

    # 通过实际事件渲染驱动审批和取消交互，不调用真实模型
    async def send_command(self, method, params):
        self.calls.append((method, params))
        if method == "session.send_message":
            self.done = asyncio.get_running_loop().create_future()
            self.app._handle_event({"type": "run.started", "run_id": "r", "goal": "goal"})
            self.started.set()
            await self.done
            return {"run_id": "r"}
        if method in {"permission.respond", "run.cancel"}:
            success = method == "permission.respond"
            self.app._handle_event(
                {
                    "type": "run.finished",
                    "run_id": "r",
                    "status": "success" if success else "failed",
                    "steps": 1,
                    "reason": "" if success else "cancelled",
                }
            )
            self.app._handle_event(
                {"type": "session.waiting_for_input", "session_id": "s", "last_run_id": "r"}
            )
            if self.done and not self.done.done():
                if success:
                    self.done.set_result(None)
                else:
                    self.done.set_exception(IpcError(-32800, "Request cancelled"))
            return {"cancelled": True}
        return {}


# 功能：Ctrl+C 在任务运行中取消当前 Run，界面保持打开并恢复输入
# 设计：使用 Textual Pilot 实际按键，而不是直接调用 action，覆盖绑定优先级和消息泵
async def test_tui_ctrl_c_cancels_without_quitting(monkeypatch):
    app = ClaudeDockTuiApp("localhost", 1)
    monkeypatch.setattr(app, "_socket_loop", AsyncMock())
    client = Client(app)
    app._client = client
    app._session_id = "s"
    async with app.run_test() as pilot:
        prompt = app._prompt()
        prompt.disabled = False
        prompt.focus()
        await pilot.press("h", "i", "enter")
        await asyncio.wait_for(client.started.wait(), 2)
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert any(
            method == "run.cancel" and params == {"run_id": "r"} for method, params in client.calls
        )
        assert app._active_run_id is None
        assert not prompt.disabled
        assert app.is_running


# 功能：真实审批控件响应快捷键，当前参数的会话授权文案与服务端语义一致
# 设计：挂载审批事件后按 a，通过客户端请求确认精确决策并检查控件被回收
async def test_tui_permission_keyboard_and_scope(monkeypatch):
    app = ClaudeDockTuiApp("localhost", 1)
    monkeypatch.setattr(app, "_socket_loop", AsyncMock())
    client = Client(app)
    app._client = client
    app._session_id = "s"
    async with app.run_test() as pilot:
        prompt = app._prompt()
        prompt.disabled = False
        prompt.focus()
        await pilot.press("h", "i", "enter")
        await asyncio.wait_for(client.started.wait(), 2)
        app._handle_event(
            {
                "type": "permission.requested",
                "tool_use_id": "tool",
                "tool_name": "bash",
                "param_preview": "echo hello",
                "run_id": "r",
            }
        )
        await pilot.pause()
        assert prompt.disabled
        assert len(app.query(PermissionSelect)) == 1
        await pilot.press("a")
        await pilot.pause()
        assert (
            "permission.respond",
            {"tool_use_id": "tool", "decision": "always_allow", "approval_token": ""},
        ) in client.calls
        assert not app._pending_permission_blocks
        assert len(app.query(PermissionSelect)) == 0
        assert not prompt.disabled
        assert "input" in PermissionBlock.LABEL_MAP["always_allow"]


# 功能：重放的历史审批请求不能再次生成可操作审批控件
# 设计：对同一工具 ID 发送历史与重复实时事件，确保前端不出现竞争审批
async def test_tui_replayed_approval_is_not_actionable(monkeypatch):
    app = ClaudeDockTuiApp("localhost", 1)
    monkeypatch.setattr(app, "_socket_loop", AsyncMock())
    async with app.run_test() as pilot:
        event = {
            "type": "permission.requested",
            "tool_use_id": "tool",
            "tool_name": "bash",
            "run_id": "r",
        }
        app._handle_event({**event, "_replay": True})
        await pilot.pause()
        assert len(app.query(PermissionSelect)) == 0
        app._handle_event(event)
        app._handle_event(event)
        await pilot.pause()
        assert len(app.query(PermissionSelect)) == 1
