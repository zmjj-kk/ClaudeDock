from __future__ import annotations

import asyncio
import json
import logging
import secrets
import ssl
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from kama_claude.core.bus.envelope import (
    INTERNAL_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    HandlerError,
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcSuccess,
    make_error,
)
from kama_claude.core.trace.record import TraceRecord
from kama_claude.core.trace.writer import TraceWriter
from kama_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster

logger = logging.getLogger(__name__)

type CommandHandler = Callable[[dict[str, Any]], Awaitable[Any]]

# 每个连接处理协程中，当前正在处理的 writer（供 handler 读取连接上下文）
_writer_var: ContextVar[asyncio.StreamWriter] = ContextVar("_writer_var")


def _now() -> str:
    return datetime.now(UTC).isoformat()


# 返回当前 handler 调用所属连接的 StreamWriter
def get_connection_writer() -> asyncio.StreamWriter:
    return _writer_var.get()


_MAX_LINE_BYTES = 1024 * 1024  # 64 MB per frame，兼容 MCP 大文件工具结果


class SocketServer:
    def __init__(
        self,
        host: str,
        port: int,
        broadcaster: IpcEventBroadcaster | None = None,
        trace: TraceWriter | None = None,
        on_disconnect: Callable[[asyncio.StreamWriter], Awaitable[None]] | None = None,
        auth_token: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
        max_connections: int = 64,
        max_requests: int = 32,
        request_timeout: float = 3600.0,
    ) -> None:
        self._ssl_context = ssl_context
        self._auth_token = auth_token
        self._max_connections = max_connections
        self._max_requests = max_requests
        self._request_timeout = request_timeout
        self._connections: set[asyncio.Task[Any]] = set()
        self._requests: dict[asyncio.StreamWriter, set[asyncio.Task[None]]] = {}
        self._host = host
        self._port = port
        self._handlers: dict[str, CommandHandler] = {}
        self._server: asyncio.AbstractServer | None = None
        self._broadcaster = broadcaster
        self._trace = trace
        self._on_disconnect = on_disconnect
        self._active_writers: set[asyncio.StreamWriter] = set()
        self._authenticated: set[asyncio.StreamWriter] = set()

    # 注册一个方法名对应的命令处理函数
    def register(self, method: str, handler: CommandHandler) -> None:
        self._handlers[method] = handler

    # 启动 TCP 服务器；若端口已被占用则退出进程
    async def start(self) -> str:
        try:
            _r, w = await asyncio.open_connection(self._host, self._port)
            w.close()
            await w.wait_closed()
            raise SystemExit(f"core already running at {self._host}:{self._port}")
        except (ConnectionRefusedError, OSError):
            pass

        self._server = await asyncio.start_server(
            self._handle_connection,
            host=self._host,
            port=self._port,
            limit=_MAX_LINE_BYTES,
            ssl=self._ssl_context,
        )
        return f"{self._host}:{self._port}"

    # 关闭服务器：先断开所有活跃连接，再等待服务器完全关闭（最多 2 秒）
    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        for writer in list(self._active_writers):
            writer.close()
        tasks = list(self._connections)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=5.0)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.wait_for(self._server.wait_closed(), timeout=5.0)
        self._server = None

    # 处理单个客户端连接，完成后关闭写流
    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername", "<unknown>")
        logger.debug("client connected: %s", peer)
        if len(self._active_writers) >= self._max_connections:
            writer.close()
            await writer.wait_closed()
            return
        connection = asyncio.current_task()
        assert connection is not None
        self._connections.add(connection)
        self._requests[writer] = set()
        self._active_writers.add(writer)
        try:
            await self._read_loop(reader, writer)
        finally:
            requests = list(self._requests.pop(writer, set()))
            for task in requests:
                task.cancel()
            try:
                if requests:
                    await asyncio.gather(*requests, return_exceptions=True)
            finally:
                self._connections.discard(connection)
                self._active_writers.discard(writer)
                self._authenticated.discard(writer)
                if self._broadcaster is not None:
                    self._broadcaster.unsubscribe(writer)
                writer.close()
                if self._on_disconnect is not None:
                    try:
                        await self._on_disconnect(writer)
                    except Exception:
                        logger.exception("on_disconnect callback failed peer=%s", peer)
                logger.debug("client disconnected: %s", peer)

    # 持续读取换行分隔的 JSON 行并逐行分发处理
    async def _read_loop(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        credit = 64.0
        updated = time.monotonic()
        while True:
            try:
                line = await asyncio.wait_for(
                    reader.readline(),
                    timeout=self._request_timeout
                    if writer in self._authenticated or self._auth_token is None
                    else 10.0,
                )
            except TimeoutError:
                return
            except (asyncio.LimitOverrunError, ValueError):
                await self._send(writer, make_error(None, INVALID_REQUEST, "Request too large"))
                return

            if not line:
                return

            now = time.monotonic()
            credit = min(64.0, credit + (now - updated) * 64)
            updated = now
            if credit < 1:
                await self._send(writer, make_error(None, -32002, "Request rate exceeded"))
                return
            credit -= 1

            # 每条命令独立作为 task 执行，避免长时间运行的 handler（如 session.send_message）
            # 阻塞读循环，使 permission.respond 等并发命令能被及时处理
            requests = self._requests[writer]
            if len(requests) >= self._max_requests:
                await self._send(writer, make_error(None, -32002, "Too many requests"))
                return
            task = asyncio.create_task(self._dispatch_request(line, writer))
            requests.add(task)
            task.add_done_callback(requests.discard)

    # 为每次请求应用总超时并回收断线时的发送异常
    async def _dispatch_request(self, line: bytes, writer: asyncio.StreamWriter) -> None:
        try:
            async with asyncio.timeout(self._request_timeout):
                await self._handle_line(line, writer)
        except TimeoutError:
            writer.close()
        except (ConnectionError, OSError):
            writer.close()

    # 解析单行 JSON-RPC 请求并调用对应 handler，将结果或错误写回客户端
    async def _handle_line(self, line: bytes, writer: asyncio.StreamWriter) -> None:
        try:
            raw: Any = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            await self._send(writer, make_error(None, PARSE_ERROR, "Malformed JSON frame"))
            return

        try:
            req = JsonRpcRequest.model_validate(raw)
        except ValidationError:
            await self._send(writer, make_error(None, INVALID_REQUEST, "Invalid Request"))
            return

        token = req.params.pop("_auth_token", "")
        if self._auth_token is not None and (
            not isinstance(token, str)
            or not secrets.compare_digest(token.encode(), self._auth_token.encode())
        ):
            await self._send(writer, make_error(req.id, -32001, "Authentication required"))
            return
        self._authenticated.add(writer)

        if self._trace is not None:
            client_id = str(writer.get_extra_info("peername", "<unknown>"))
            self._trace.emit(
                TraceRecord(
                    ts=_now(),
                    direction="CLIENT→CORE",
                    layer="ipc",
                    kind="command",
                    client_id=client_id,
                    data={"method": req.method, "id": req.id, "params": req.params},
                )
            )

        handler = self._handlers.get(req.method)
        if handler is None:
            await self._send(
                writer,
                make_error(req.id, METHOD_NOT_FOUND, f"Method not found: {req.method}"),
            )
            return

        _writer_var.set(writer)
        try:
            result = await handler(req.params)
        except asyncio.CancelledError:
            if not writer.is_closing():
                await self._send(writer, make_error(req.id, -32800, "Request cancelled"))
            raise
        except HandlerError as e:
            await self._send(writer, make_error(req.id, e.code, str(e), e.data))
            return
        except ValidationError as e:
            await self._send(
                writer,
                make_error(
                    req.id,
                    INVALID_REQUEST,
                    "Invalid params",
                    e.errors(include_input=False, include_context=False),
                ),
            )
            return
        except Exception as e:
            logger.exception("handler %s raised: %s", req.method, e)
            await self._send(writer, make_error(req.id, INTERNAL_ERROR, "Internal error"))
            return

        result_data: Any = result.model_dump() if isinstance(result, BaseModel) else result
        try:
            await self._send(writer, JsonRpcSuccess(id=req.id, result=result_data))
        except (ConnectionResetError, BrokenPipeError, OSError):
            logger.debug("client disconnected before response for %s", req.method)

    # 将 pydantic 消息序列化为 JSON 行并写入流，随后刷新缓冲区
    async def _send(self, writer: asyncio.StreamWriter, msg: BaseModel) -> None:
        writer.write(msg.model_dump_json().encode() + b"\n")
        await asyncio.wait_for(writer.drain(), timeout=5.0)
        if self._trace is not None:
            kind = "error" if isinstance(msg, JsonRpcError) else "response"
            client_id = str(writer.get_extra_info("peername", "<unknown>"))
            self._trace.emit(
                TraceRecord(
                    ts=_now(),
                    direction="CORE→CLIENT",
                    layer="ipc",
                    kind=kind,
                    client_id=client_id,
                    data=msg.model_dump(),
                )
            )
