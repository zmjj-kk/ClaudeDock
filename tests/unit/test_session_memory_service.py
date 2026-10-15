from __future__ import annotations

from pathlib import Path

from kama_claude.core.bus.events import LlmTokenEvent, RunStartedEvent, ToolCallFinishedEvent
from kama_claude.core.events.bus import EventBus
from kama_claude.core.session.memory import SessionNoteState, SessionNoteStore
from kama_claude.core.session.memory_service import SessionMemoryService


# 功能：验证陈旧后台笔记无法覆盖较新版本
# 设计：两个候选基于同一版本提交，第二次必须被 CAS 拒绝
def test_notes_reject_stale_candidate(tmp_path: Path) -> None:
    store = SessionNoteStore(tmp_path)
    first = SessionNoteState(goal="goal")
    stale = SessionNoteState(goal="old goal")
    assert store.compare_and_swap(first, 0)
    assert not store.compare_and_swap(stale, 0)
    assert store.read().goal == "goal" and store.read().revision == 1


# 功能：验证会话笔记只记录工具执行证据，助手声称通过测试不变成已验证事实
# 设计：混入助手文本与工具成功事件，检查已验证集合的唯一来源
async def test_memory_uses_tool_evidence_only(tmp_path: Path) -> None:
    service = SessionMemoryService(tmp_path, "s", tmp_path)
    bus = EventBus()
    await service.observe(RunStartedEvent(run_id="r", goal="goal", ts="2026-10-15"), bus)
    await service.observe(LlmTokenEvent(run_id="r", token="All tests passed", ts="2026-10-15"), bus)
    await service.observe(ToolCallFinishedEvent(
        run_id="r", tool_use_id="call", tool_name="read_file",
        elapsed_ms=1, output="contents", ts="2026-10-15",
    ), bus)
    await service.aclose()
    state = service.store.read()
    assert len(state.completed_verified) == 1
    assert state.completed_verified[0]["tool_use_id"] == "call"
    assert "All tests passed" not in state.render()
