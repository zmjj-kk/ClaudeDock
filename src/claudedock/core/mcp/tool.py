from __future__ import annotations

from copy import deepcopy
from typing import Any

from claudedock.core.mcp.client import (
    McpClient,
    McpServerUnavailableError,
    McpToolDef,
    McpToolError,
)
from claudedock.core.security.capabilities import Capability
from claudedock.core.security.context import SecurityContext
from claudedock.core.tools.base import BaseTool, ToolResult
from claudedock.core.tools.paths import resolve_inside


# 将 MCP 工具包装为 BaseTool，使 ToolRegistry 可透明调用
class McpTool(BaseTool):
    retryable_errors = frozenset()
    risk_level = "external"
    params_model = None  # input_schema 来自 MCP tool_def，不使用 pydantic model

    # 初始化 MCP 工具包装器，工具名以 server_name__ 为前缀防止命名冲突
    def __init__(
        self,
        client: McpClient,
        server_name: str,
        tool_def: McpToolDef,
        *,
        server_id: str = "",
        capabilities: frozenset[Capability] = frozenset(),
        trusted_registration: bool = False,
    ) -> None:
        self.server_id = server_id or f"untrusted:{server_name}"
        self.capabilities = capabilities
        self.trusted_registration = trusted_registration and bool(capabilities) and bool(server_id)
        self._client = client
        self._server_name = server_name
        self._tool_def = deepcopy(tool_def)
        self.name = f"{server_name}__{tool_def.name}"
        self.description = tool_def.description or f"MCP tool from {server_name}"
        self.input_schema: dict[str, Any] = deepcopy(tool_def.input_schema) or {
            "type": "object",
            "properties": {},
        }

    # 独立代理校验参数体积、路径和外发目标；能力仅由受信配置授予
    def check_permissions(self, context: SecurityContext, params: dict[str, object]) -> str | None:
        import json

        if len(json.dumps(params, ensure_ascii=False).encode()) > 65536:
            return "mcp_payload_limit"
        if Capability.EXTERNAL_EFFECT in self.capabilities:
            return "trusted_business_backend_required"

        # 未建可信网络资源代理前拒绝 URL 与身份提升参数，不能信任远端自报资源
        def validate(value: object, key: str = "") -> str | None:
            if key.casefold() in {"is_admin", "admin", "role", "principal_id", "user_id"}:
                return "mcp_identity_override"
            if isinstance(value, str):
                if "://" in value or key.casefold() in {"url", "endpoint", "host"}:
                    return "mcp_network_broker_required"
                if key.casefold() in {"path", "file", "filename", "directory", "cwd"}:
                    if resolve_inside(context.project_root, value).path is None:
                        return "mcp_resource_outside_scope"
            elif isinstance(value, dict):
                for child_key, child in value.items():
                    rejection = validate(child, str(child_key))
                    if rejection:
                        return rejection
            elif isinstance(value, list):
                if len(value) > 100:
                    return "mcp_batch_limit"
                for child in value:
                    rejection = validate(child, key)
                    if rejection:
                        return rejection
            return None

        return validate(params)

    # 调用 MCP server 上的工具，连接不可用或工具执行失败时返回 is_error=True
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        try:
            if self.trusted_registration:
                definitions = await self._client.list_tools()
                matches = [item for item in definitions if item.name == self._tool_def.name]
                if len(matches) != 1 or matches[0].input_schema != self.input_schema:
                    return ToolResult(
                        "MCP schema changed; registration and approval revoked",
                        is_error=True,
                        error_type="permission_denied",
                    )
            content = await self._client.call_tool(self._tool_def.name, dict(params))
            return ToolResult(content=content)
        except McpServerUnavailableError as exc:
            return ToolResult(
                content=f"mcp server '{self._server_name}' unavailable: {exc}",
                is_error=True,
                error_type="runtime_error",
            )
        except McpToolError as exc:
            return ToolResult(
                content=f"mcp tool '{self.name}' error: {exc}",
                is_error=True,
                error_type="runtime_error",
            )
        except Exception as exc:
            return ToolResult(
                content=f"mcp tool '{self.name}' unexpected error: {exc}",
                is_error=True,
                error_type="runtime_error",
            )
