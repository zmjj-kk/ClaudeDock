from __future__ import annotations

import asyncio
import json
import os
import ssl
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from kama_claude.core.bus.envelope import JsonRpcRequest
from kama_claude.core.transport.auth import read_token

type EventHandler = Callable[[dict[str, Any]], Awaitable[None]]

_MAX_LINE_BYTES = 64 * 1024 * 1024  # 64 MB per frame，兼容 MCP 大文件工具结果


class IpcError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code


class SocketClient:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        token: str | None = None,
        request_timeout: float = 3600.0,
        cursors: dict[str, int] | None = None,
    ) -> None:
        self._active_runs: set[str] = set()
        self._cursors = cursors if cursors is not None else {}
        self._token = token
        self._request_timeout = request_timeout
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._event_handlers: list[EventHandler] = []

    # 建立到 core 守护进程的 TCP 连接
    async def connect(self) -> None:
        tls_context = None
        ca_file = os.environ.get("KAMA_TLS_CA")
        if (
            ca_file
            or os.environ.get("KAMA_TLS_ENABLE") == "1"
            or self._host not in {"127.0.0.1", "::1", "localhost"}
        ):
            tls_context = ssl.create_default_context(cafile=ca_file)
            tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port, limit=_MAX_LINE_BYTES, ssl=tls_context
        )

    # 关闭 TCP 连接并等待底层 socket 释放
    async def close(self) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        if self._writer is not None:
            self._writer.close()
            try:
                await asyncio.wait_for(self._writer.wait_closed(), timeout=1.0)
            except TimeoutError:
                pass

    # 注册服务器推送事件的回调，可多次调用以添加多个 handler
    def on_event(self, handler: EventHandler) -> None:
        self._event_handlers.append(handler)

    # 发送 JSON-RPC 命令并等待响应，成功返回 result dict，失败抛出 IpcError
    async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._writer is None:
            raise RuntimeError("not connected — call connect() first")
        req_id = str(uuid.uuid4())
        auth = self._token
        if auth is None:
            try:
                auth = read_token(self._port)
            except FileNotFoundError:
                auth = None
        parameters = dict(params)
        if auth is not None:
            parameters["_auth_token"] = auth
        request = JsonRpcRequest(id=req_id, method=method, params=parameters)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            async with asyncio.timeout(self._request_timeout):
                self._writer.write(request.model_dump_json().encode() + b"\n")
                await self._writer.drain()
                return await fut
        finally:
            self._pending.pop(req_id, None)
            if not fut.done():
                fut.cancel()

    # 持续读取服务器消息，分发 RPC 响应到 pending future 或事件到 event handler
    async def run_event_loop(self) -> None:
        if self._reader is None:
            raise RuntimeError("not connected — call connect() first")
        try:
            while True:
                try:
                    line = await self._reader.readline()
                except (ConnectionResetError, OSError):
                    break
                except (ValueError, asyncio.LimitOverrunError):
                    # 单行超出 limit；丢弃本行，继续读取后续消息
                    continue
                if not line:
                    break
                await self._dispatch(line)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.cancel()
            self._pending.clear()

    # 解析单行消息并路由到 pending future（RPC 响应）或 event handler（服务器推送）
    async def _dispatch(self, line: bytes) -> None:
        try:
            msg: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            return

        if "jsonrpc" in msg:
            req_id: str | None = msg.get("id")
            if req_id and req_id in self._pending:
                fut = self._pending.pop(req_id)
                if not fut.done():
                    if "error" in msg:
                        err = msg["error"]
                        fut.set_exception(
                            IpcError(err.get("code", -1), err.get("message", "unknown"))
                        )
                    else:
                        fut.set_result(msg.get("result") or {})
        elif msg.get("kind") == "event":
            event_data: dict[str, Any] = msg.get("event", {})
            sequence = msg.get("sequence", 0)
            run_id = event_data.get("run_id")
            if run_id and isinstance(sequence, int) and sequence > 0:
                if sequence <= self._cursors.get(run_id, 0):
                    return
                self._cursors[run_id] = sequence
                if len(self._cursors) > 512:
                    oldest = next(
                        (key for key in self._cursors if key not in self._active_runs), None
                    )
                    if oldest is not None:
                        self._cursors.pop(oldest)
            if run_id:
                if event_data.get("type") in {"run.started", "subagent.started"}:
                    self._active_runs.add(run_id)
                elif event_data.get("type") in {"run.finished", "subagent.finished"}:
                    self._active_runs.discard(run_id)
            for handler in self._event_handlers:
                await handler(event_data)
