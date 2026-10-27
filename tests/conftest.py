from __future__ import annotations

import asyncio
import os
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest


@pytest.fixture
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return port  # socket released; daemon can bind to this port


@pytest.fixture
async def running_daemon(
    free_port: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[subprocess.Popen[bytes], None]:
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("CLAUDEDOCK_IPC_TOKEN", token)
    env = os.environ.copy()
    env["CLAUDEDOCK_SESSIONS_DIR"] = str(tmp_path / "sessions")
    env["CLAUDEDOCK_RAG_ENABLED"] = "0"
    env["CLAUDEDOCK_MEMORY_ENABLED"] = "0"
    env["CLAUDEDOCK_TRACE_ENABLED"] = "0"
    env["ANTHROPIC_API_KEY"] = ""
    env["CLAUDEDOCK_PORT"] = str(free_port)
    env["CLAUDEDOCK_LOG_FILE"] = ""
    env["CLAUDEDOCK_LOG_LEVEL"] = "WARNING"

    proc = subprocess.Popen([sys.executable, "-m", "claudedock.core"], env=env)

    deadline = time.monotonic() + 3.0
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
        pytest.fail("Daemon did not start within 3 seconds")

    yield proc

    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
