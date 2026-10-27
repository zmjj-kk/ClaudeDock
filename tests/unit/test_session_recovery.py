from __future__ import annotations

from pathlib import Path

import pytest

from claudedock.core.bus.envelope import HandlerError
from claudedock.core.events.bus import EventBus
from claudedock.core.runner import RunOutcome
from claudedock.core.session.manager import (
    SESSION_NOT_FOUND,
    RestoreResult,
    SessionManager,
)
from claudedock.core.session.model import Session
from claudedock.core.session.store import SessionStore


class _Runner:
    # 模拟 AgentRunner，将 run 新消息写入 thread 后返回成功
    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        system_prompt_override: str | None = None,
        tool_whitelist: list[str] | None = None,
    ) -> RunOutcome:
        assert run_id is not None
        assert session is not None
        assert store is not None
        store.append_messages(
            session.id,
            [{"role": "assistant", "content": [{"type": "text", "text": f"done {goal}"}]}],
            run_id,
        )
        return RunOutcome(status="success", result="done", reason=None)


# 构造一个指定 id/状态/run_ids 的 Session，供直接落盘模拟上次进程残留
def _session(sid: str, status: str, run_ids: list[str] | None = None) -> Session:
    return Session(
        id=sid,
        mode="chat",
        status=status,
        title="prev",
        created_at="t1",
        updated_at="t2",
        run_ids=run_ids or [],
    )


# 功能：验证 restore 能扫描 meta.json 并将未关闭的 session 重建进内存索引
# 设计：绕过 create 直接 write_meta 落盘后用新 SessionManager 模拟重启，断言恢复条数与 get_history 可用
async def test_restore_loads_open_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.write_meta(_session("sess-open", "waiting_for_input"))

    manager = SessionManager(SessionStore(tmp_path), lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    result = await manager.restore()

    assert result.restored == 1
    assert await manager.get_history("sess-open") == []


# 功能：验证 status=closed 的 session 在 restore 时被忽略，不会被注册
# 设计：同时落盘一开一闭两个 session，restore 后只应恢复开放者，closed 的 get_history 抛 SESSION_NOT_FOUND
async def test_restore_skips_closed_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.write_meta(_session("sess-open", "waiting_for_input"))
    store.write_meta(_session("sess-closed", "closed"))

    manager = SessionManager(SessionStore(tmp_path), lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    result = await manager.restore()

    assert result.restored == 1
    assert await manager.get_history("sess-open") == []
    with pytest.raises(HandlerError) as exc:
        await manager.get_history("sess-closed")
    assert exc.value.code == SESSION_NOT_FOUND


# 功能：验证损坏的 meta.json 被跳过并计入 skipped，不阻塞其它合法 session 的恢复
# 设计：写入非法 JSON 的 meta.json，restore 应只恢复可解析者并返回 skipped=1，坏 id 仍不可访问
async def test_restore_skips_broken_meta(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.write_meta(_session("sess-good", "waiting_for_input"))
    bad_dir = tmp_path / "sess-bad"
    bad_dir.mkdir()
    (bad_dir / "meta.json").write_text("{ not valid json ", encoding="utf-8")

    manager = SessionManager(SessionStore(tmp_path), lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    result = await manager.restore()

    assert result.restored == 1
    assert result.skipped == 1
    assert await manager.get_history("sess-good") == []
    with pytest.raises(HandlerError):
        await manager.get_history("sess-bad")


# 功能：验证恢复后的 session 能读回 thread.jsonl 中的完整历史上下文
# 设计：落盘 user/assistant 两条 thread 后重启，get_history 应返回剥离 ts/run_id 后的 API messages
async def test_restored_session_reads_thread_history(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.write_meta(_session("sess-open", "waiting_for_input"))
    store.append_message("sess-open", "user", "昨天的问题")
    store.append_message(
        "sess-open",
        "assistant",
        [{"type": "text", "text": "昨天的回答"}],
        run_id="run-1",
    )

    manager = SessionManager(SessionStore(tmp_path), lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    await manager.restore()

    assert await manager.get_history("sess-open") == [
        {"role": "user", "content": "昨天的问题"},
        {"role": "assistant", "content": [{"type": "text", "text": "昨天的回答"}]},
    ]


# 功能：验证恢复后的 waiting_for_input session 能直接 send_message 续接，且历史被完整保留
# 设计：重启后向旧 session 发送新消息，mock runner 追加 assistant 回复，断言 thread 由旧历史 + 新一轮拼接而成
async def test_restored_session_accepts_followup_message(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.write_meta(_session("sess-open", "waiting_for_input"))
    store.append_message("sess-open", "user", "昨天的问题")

    manager = SessionManager(SessionStore(tmp_path), lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    await manager.restore()

    await manager.send_message("sess-open", "续聊")

    messages = store.read_messages("sess-open")
    # 中断残留的 user 与新 follow-up 的 user 相邻，read_messages 应合并为一条，避免 API 报错
    assert messages[0] == {"role": "user", "content": "昨天的问题\n\n续聊"}
    assert messages[1]["role"] == "assistant"


# 功能：验证 restore 检测到 thread 以 user 结尾的中断 run，补发 cancelled 事件并归一化状态
# 设计：落盘 active + run_ids 且 thread 末尾为 orphan user，restore 后 interrupted=1，状态改为 waiting_for_input
async def test_restore_detects_interruption(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.write_meta(_session("sess-int", "active", run_ids=["run-interrupted"]))
    store.append_message("sess-int", "user", "被打断的问题")

    events: list[object] = []
    bus = EventBus()

    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)
    manager = SessionManager(SessionStore(tmp_path), lambda: _Runner(), bus)  # type: ignore[arg-type]
    result = await manager.restore()

    assert result.interrupted == 1
    assert result.restored == 1
    assert store.read_meta("sess-int").status == "waiting_for_input"
    finished = [e for e in events if getattr(e, "type", "") == "run.finished"]
    assert len(finished) == 1
    assert getattr(finished[0], "reason", "") == "interrupted"  # type: ignore[attr-defined]
    assert getattr(finished[0], "run_id", "") == "run-interrupted"  # type: ignore[attr-defined]


# 功能：验证 restore 幂等，重复调用不再重复注册或重复补发事件
# 设计：连续 restore 两次，第二次应返回全 0，且事件只补发一次
async def test_restore_is_idempotent(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.write_meta(_session("sess-open", "waiting_for_input"))

    manager = SessionManager(SessionStore(tmp_path), lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    first = await manager.restore()
    second = await manager.restore()

    assert first.restored == 1
    assert second == RestoreResult(restored=0, skipped=0, interrupted=0)


# 功能：验证 list_sessions 返回内存中的全部 session（含恢复的）
# 设计：恢复一个落盘 session 后新建一个，list_sessions 应同时包含两者，供 session.list 命令消费
async def test_list_sessions_includes_restored(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.write_meta(_session("sess-old", "waiting_for_input"))

    manager = SessionManager(SessionStore(tmp_path), lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    await manager.restore()
    await manager.create("chat")

    ids = {s.id for s in manager.list_sessions()}
    assert "sess-old" in ids
    assert len(ids) == 2
