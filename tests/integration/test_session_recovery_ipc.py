from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from kama_claude.core.transport.auth import read_token


# 发送一条 JSON-RPC 请求并返回响应对象
async def _send_recv(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    method: str,
    params: dict,
    req_id: str = "1",
) -> dict:
    req = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": method,
        "params": {**params, "_auth_token": read_token(writer.get_extra_info("peername")[1])},
    }
    writer.write((json.dumps(req) + "\n").encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    return json.loads(line)


# 落盘一个 Session 的 meta.json 与 thread.jsonl，模拟上次 daemon 进程残留
def _seed_session(root: Path, sid: str, status: str, messages: list[dict]) -> None:
    sdir = root / sid
    sdir.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": sid,
        "mode": "chat",
        "status": status,
        "title": "prev",
        "created_at": "t1",
        "updated_at": "t2",
        "run_ids": [],
    }
    (sdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    with (sdir / "thread.jsonl").open("w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")


# 在临时目录先落盘历史 session，再启动指向该目录的 daemon，退出时回收子进程
@pytest.fixture
async def recovery_daemon(
    free_port: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[tuple[subprocess.Popen[bytes], Path], None]:
    # 先于 daemon 启动落盘，确保 restore 扫描到历史 session
    _seed_session(
        tmp_path,
        "sess-prev",
        "waiting_for_input",
        [
            {"ts": "t1", "role": "user", "content": "昨天的问题"},
            {
                "ts": "t2",
                "role": "assistant",
                "content": [{"type": "text", "text": "昨天的回答"}],
            },
        ],
    )
    _seed_session(tmp_path, "sess-closed", "closed", [])

    monkeypatch.setenv("KAMA_IPC_TOKEN", secrets.token_urlsafe(32))
    env = os.environ.copy()
    env["KAMA_PORT"] = str(free_port)
    env["KAMA_SESSIONS_DIR"] = str(tmp_path)
    env["KAMA_TRACE_ENABLED"] = "0"
    env["KAMA_LOG_FILE"] = ""
    env["KAMA_LOG_LEVEL"] = "WARNING"
    env["ANTHROPIC_API_KEY"] = "dummy"  # 仅用于通过启动时 provider 构造，不实际调用 LLM

    proc = subprocess.Popen([sys.executable, "-m", "kama_claude.core"], env=env)

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
            writer.close()
            await writer.wait_closed()
            break
        except (ConnectionRefusedError, OSError):
            pass
    else:
        proc.terminate()
        proc.wait()
        pytest.fail("Daemon did not start within 5 seconds")

    yield proc, tmp_path

    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# 功能：验证 daemon 重启后通过 session.list / session.get_history 恢复磁盘上未关闭的旧 session
# 设计：启动前落盘一个开放 session（含 thread）+ 一个 closed session，断言 list 只含开放者且 history 读回完整 thread
async def test_session_recovery_over_ipc(
    recovery_daemon: tuple[subprocess.Popen[bytes], Path],
    free_port: int,
) -> None:
    _proc, _sessions_dir = recovery_daemon

    reader, writer = await asyncio.open_connection("127.0.0.1", free_port)

    listed = await _send_recv(reader, writer, "session.list", {}, req_id="list")
    assert "result" in listed, listed
    ids = {s["id"] for s in listed["result"]["sessions"]}
    assert "sess-prev" in ids
    assert "sess-closed" not in ids  # closed 不应被恢复

    history = await _send_recv(
        reader,
        writer,
        "session.get_history",
        {"session_id": "sess-prev"},
        req_id="history",
    )
    assert history["result"]["messages"] == [
        {"role": "user", "content": "昨天的问题"},
        {"role": "assistant", "content": [{"type": "text", "text": "昨天的回答"}]},
    ]

    writer.close()
    await writer.wait_closed()
