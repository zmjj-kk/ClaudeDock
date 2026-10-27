from __future__ import annotations

from pathlib import Path

import pytest

from claudedock.core.compact.projection import ProjectionStore
from claudedock.core.session.store import SessionStore


# 功能：验证重复压缩保留完整历史，重启只恢复最新投影与新消息
# 设计：比较原始日志字节并新建存储实例，覆盖持久化而非仅内存状态
def test_projection_keeps_transcript(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.append_message("s", "user", "original")
    store.append_message("s", "assistant", "answer")
    thread = store.session_dir("s") / "thread.jsonl"
    original = thread.read_bytes()
    summary = [{"role": "user", "content": "summary"}, {"role": "assistant", "content": "ack"}]
    for _ in range(3):
        store.write_compacted("s", summary)
    assert thread.read_bytes() == original
    store.append_message("s", "user", "new constraint")
    resumed = SessionStore(tmp_path)
    assert resumed.read_messages("s") == summary + [{"role": "user", "content": "new constraint"}]
    assert resumed.read_history("s")[0]["content"] == "original"


# 功能：验证版本冲突拒绝写入，损坏最新版本回退旧完整版本
# 设计：真实修改不可变版本文件，检查版本指针不会混合半提交内容
def test_revision_conflict_and_recovery(tmp_path: Path) -> None:
    store = ProjectionStore(tmp_path)
    messages = [{"role": "user", "content": "goal"}]
    first = store.commit(messages, 1, expected_revision=0)
    with pytest.raises(ValueError, match="revision conflict"):
        store.commit(messages, 1, expected_revision=0)
    store.commit(messages, 1, expected_revision=first.revision)
    (tmp_path / "compact/projection_2.json").write_text("broken")
    restored = store.read()
    assert restored is not None and restored.revision == 1
