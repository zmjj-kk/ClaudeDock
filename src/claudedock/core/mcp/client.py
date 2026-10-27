from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from claudedock.core.monitoring.telemetry import observe

log = logging.getLogger(__name__)


class McpServerUnavailableError(Exception):
    pass


class McpToolError(Exception):
    """MCP server 返回的应用层错误（连接正常，但工具调用失败）"""

    pass


@dataclass
class McpToolDef:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


# 通过 stdio 或 TCP 与 MCP server 通信的 JSON-RPC 2.0 客户端
class McpClient:
    # 构造客户端；read_timeout 控制单次响应的读取超时（秒）
    def __init__(self, read_timeout: float = 30.0) -> None:
        self._id = 0
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._tcp_writer: asyncio.StreamWriter | None = None
        self._transport = ""
        self._lock = asyncio.Lock()
        self._stderr_task: asyncio.Task[None] | None = None
        self._read_timeout = read_timeout
        self.protocol_version = ""
        self.capabilities: dict[str, Any] = {}

    _STREAM_LIMIT = 64 * 1024 * 1024  # 64 MB，防止大响应触发 LimitOverrunError

    # 启动 stdio 子进程并完成 MCP initialize 握手
    async def connect_stdio(
        self,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
    ) -> None:
        import os

        merged_env = {
            k: v
            for k, v in os.environ.items()
            if k in {"PATH", "LANG", "LC_ALL", "TZ", "HOME", "TMPDIR"}
        }
        merged_env.update(env or {})
        self._proc = await asyncio.create_subprocess_exec(
            command,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
            limit=self._STREAM_LIMIT,
        )
        self._reader = self._proc.stdout
        self._transport = "stdio"
        # 后台持续读取 stderr，防止管道缓冲区满导致子进程阻塞
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            await self._initialize()
        except BaseException:
            await self.close()
            raise

    # 通过 TCP 连接到 MCP server 并完成 initialize 握手
    async def connect_tcp(self, host: str, port: int) -> None:
        self._reader, tcp_writer = await asyncio.open_connection(
            host, port, limit=self._STREAM_LIMIT
        )
        self._tcp_writer = tcp_writer
        self._transport = "tcp"
        try:
            await self._initialize()
        except BaseException:
            await self.close()
            raise

    # 发送 initialize 请求完成 MCP 握手
    async def _initialize(self) -> None:
        result = await self._call(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "claudedock-claude", "version": "0.1"},
            },
        )
        version = result.get("protocolVersion")
        capabilities = result.get("capabilities")
        info = result.get("serverInfo")
        if version not in {"2024-11-05", "2025-03-26", "2025-06-18"}:
            raise McpServerUnavailableError("unsupported MCP protocol version")
        if not isinstance(capabilities, dict) or not isinstance(info, dict):
            raise McpServerUnavailableError("invalid MCP initialization response")
        if not isinstance(info.get("name"), str) or not isinstance(info.get("version"), str):
            raise McpServerUnavailableError("invalid MCP server identity")
        self.protocol_version = version
        self.capabilities = capabilities
        await self._notify("notifications/initialized", {})

    # 列出 MCP server 提供的工具定义
    async def list_tools(self) -> list[McpToolDef]:
        if self.protocol_version and "tools" not in self.capabilities:
            return []
        response = await self._call("tools/list", {})
        if not isinstance(response.get("tools", []), list):
            raise McpToolError("tools/list must contain an array")
        tools = []
        for t in response.get("tools", []):
            if (
                not isinstance(t, dict)
                or not isinstance(t.get("name"), str)
                or not t["name"]
                or not isinstance(t.get("inputSchema"), dict)
            ):
                raise McpToolError("invalid MCP tool definition")
            tools.append(
                McpToolDef(
                    name=t.get("name", ""),
                    description=t.get("description", ""),
                    input_schema=t.get("inputSchema", {}),
                )
            )
        return tools

    # 调用 MCP 工具，拼接所有 text 内容；非 text 内容用占位符保留，避免静默丢失；
    # 连接异常抛 McpServerUnavailableError，工具错误抛 McpToolError
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        response = await self._call("tools/call", {"name": name, "arguments": arguments})
        if response.get("isError") is True:
            raise McpToolError("MCP tool returned isError")
        if not isinstance(response.get("content", []), list):
            raise McpToolError("MCP tool content must be an array")
        parts: list[str] = []
        for item in response.get("content", []):
            if not isinstance(item, dict):
                raise McpToolError("invalid MCP content block")
            ctype = item.get("type")
            if ctype == "text":
                parts.append(str(item.get("text", "")))
            elif ctype == "image":
                parts.append("[image content]")
            elif ctype == "audio":
                parts.append("[audio content]")
            elif ctype == "resource":
                resource = item.get("resource", {})
                parts.append(str(resource.get("text", "[resource content]")))
        return "\n".join(parts)

    # 后台任务：持续读取 stderr 并记录日志，防止管道缓冲区满
    async def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                stderr_line = line.decode(errors="replace").rstrip()
                if stderr_line:
                    log.debug("mcp stderr: %s", stderr_line)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.debug("mcp stderr drain stopped", exc_info=True)

    # 关闭连接并终止 stdio 子进程；结束后清理引用以支持安全重连与幂等关闭
    async def close(self) -> None:
        # 先取消 stderr 读取任务
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None
        if self._transport == "stdio" and self._proc is not None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except Exception:
                try:
                    self._proc.kill()
                    await self._proc.wait()
                except Exception:
                    pass
        elif self._transport == "tcp":
            if self._tcp_writer is not None:
                self._tcp_writer.close()
                try:
                    await self._tcp_writer.wait_closed()
                except Exception:
                    pass
        self._proc = None
        self._reader = None
        self._tcp_writer = None
        self._transport = ""

    # 发送 JSON-RPC 请求并等待响应；id 比较用字符串兼容服务端返回字符串 id 的情况
    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            with observe("mcp.request", {"method": method}):
                async with asyncio.timeout(self._read_timeout):
                    return await self._call_response(method, params)
        except TimeoutError as exc:
            raise McpServerUnavailableError("MCP request deadline exceeded") from exc

    # 串行关联请求与响应，并忽略服务端通知
    async def _call_response(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._id += 1
        req_id = self._id
        req_id_str = str(req_id)
        request = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        async with self._lock:
            await self._write_line(json.dumps(request))
            while True:
                line = await self._read_line()
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("mcp: ignoring non-JSON line: %r", line[:200])
                    continue
                if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
                    raise McpToolError("invalid JSON-RPC response envelope")
                msg_id = msg.get("id")
                if msg_id is None:
                    # server-initiated notification，忽略
                    log.debug("mcp: received server notification: %s", msg.get("method"))
                    continue
                if str(msg_id) == req_id_str:
                    if "error" in msg:
                        err = msg["error"]
                        raise McpToolError(
                            f"{err.get('message', str(err))} (code={err.get('code')})"
                        )
                    result = msg.get("result", {})
                    if not isinstance(result, dict):
                        raise McpToolError("MCP result must be an object")
                    return result

    # 发送 JSON-RPC 通知（无响应）；与请求共用锁，避免写缓冲交错
    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        notification = {"jsonrpc": "2.0", "method": method, "params": params}
        async with self._lock:
            await self._write_line(json.dumps(notification))

    # 向 MCP server 写入一行 JSON
    async def _write_line(self, line: str) -> None:
        data = (line + "\n").encode()
        if self._transport == "stdio":
            w = self._proc.stdin if self._proc else None
            if w is None:
                raise McpServerUnavailableError("stdio writer unavailable")
            w.write(data)
            await w.drain()
        elif self._transport == "tcp":
            if self._tcp_writer is None:
                raise McpServerUnavailableError("tcp writer unavailable")
            self._tcp_writer.write(data)
            await self._tcp_writer.drain()

    # 从 MCP server 读取一行 JSON；跳过空行，仅 EOF（b""）才视为连接断开
    async def _read_line(self) -> str:
        if self._reader is None:
            raise McpServerUnavailableError("reader unavailable")
        while True:
            try:
                data = await asyncio.wait_for(self._reader.readline(), timeout=self._read_timeout)
            except TimeoutError:
                raise McpServerUnavailableError("MCP server read timeout")
            except (asyncio.LimitOverrunError, ValueError) as exc:
                raise McpServerUnavailableError(
                    f"MCP response too large (>{self._STREAM_LIMIT // 1024 // 1024}MB): {exc}"
                ) from exc
            if data == b"":
                raise McpServerUnavailableError("MCP server closed connection")
            line = data.decode(errors="replace").strip()
            if line:
                return line
