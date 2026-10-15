from __future__ import annotations

import logging
import uuid
from dataclasses import asdict
from pathlib import Path

from kama_claude.core.config import McpServerConfig
from kama_claude.core.mcp.client import McpClient
from kama_claude.core.mcp.tool import McpTool
from kama_claude.core.security.capabilities import Capability
from kama_claude.core.security.context import digest
from kama_claude.core.tools.builtin.bash import BashTool

log = logging.getLogger(__name__)


# 管理所有 MCP server 连接的生命周期：启动、工具发现、注册、关闭
class McpServerManager:
    def __init__(self) -> None:
        self._clients: dict[str, McpClient] = {}
        self._tools: list[McpTool] = []

    # 依次连接每个 MCP server，发现工具后缓存供后续 registry 使用；失败时记录日志并跳过
    async def start_all(self, servers: list[McpServerConfig]) -> None:
        for cfg in servers:
            if cfg.name in self._clients:
                log.warning("mcp: duplicate server name '%s', skipping", cfg.name)
                continue
            client: McpClient | None = None
            try:
                client = await self._connect(cfg)
                tool_defs = await client.list_tools()
                server_id = digest({"config": asdict(cfg), "connection": uuid.uuid4().hex})
                for tool_def in tool_defs:
                    grants = frozenset(
                        Capability(c) for c in cfg.tool_capabilities.get(tool_def.name, [])
                    )
                    self._tools.append(
                        McpTool(
                            client,
                            cfg.name,
                            tool_def,
                            server_id=server_id,
                            capabilities=grants,
                            trusted_registration=bool(grants),
                        )
                    )
                self._clients[cfg.name] = client
                log.info(
                    "mcp: server '%s' connected, %d tool(s) discovered",
                    cfg.name,
                    len(tool_defs),
                )
            except Exception:
                # 连接已建立但 list_tools 失败时关闭 client，避免泄漏 stdio 子进程
                if client is not None:
                    try:
                        await client.close()
                    except Exception:
                        log.warning("mcp: error closing failed client '%s'", cfg.name)
                log.exception("mcp: server '%s' failed to start, skipping", cfg.name)

    # 返回已发现的 MCP 工具列表（用于 runner 每次 run 时注入新 registry）
    def get_tools(self) -> list[McpTool]:
        return list(self._tools)

    # 关闭所有 MCP 连接并终止 stdio 子进程
    async def stop_all(self) -> None:
        for name, client in list(self._clients.items()):
            try:
                await client.close()
                log.info("mcp: server '%s' closed", name)
            except Exception:
                log.warning("mcp: error closing server '%s'", name)
        self._clients.clear()
        self._tools.clear()

    # 根据 transport 类型建立连接
    async def _connect(self, cfg: McpServerConfig) -> McpClient:
        client = McpClient(read_timeout=cfg.read_timeout)
        if cfg.transport == "stdio":
            if not cfg.command:
                raise ValueError(f"mcp server '{cfg.name}': stdio transport requires 'command'")
            import shlex

            sandbox = BashTool(root=Path.cwd())
            command = shlex.join([cfg.command, *cfg.args])
            argv = sandbox._argv(command, 120)
            await client.connect_stdio(argv[0], argv[1:], cfg.env or None)
        elif cfg.transport == "tcp":
            if cfg.host not in {"127.0.0.1", "::1"}:
                raise PermissionError("Plain TCP MCP requires a literal loopback endpoint")
            await client.connect_tcp(cfg.host, cfg.port)
        else:
            raise ValueError(f"mcp server '{cfg.name}': unknown transport '{cfg.transport}'")
        return client
