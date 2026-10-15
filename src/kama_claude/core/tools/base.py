from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from kama_claude.core.security.capabilities import Capability
from kama_claude.core.security.context import digest

if TYPE_CHECKING:
    from kama_claude.core.security.context import SecurityContext

from pydantic import BaseModel


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    # "runtime_error" | "timeout" | "schema_error" | "permission_denied"
    error_type: str | None = None


class BaseTool(ABC):
    server_id: str = "builtin"
    capabilities: frozenset[Capability] = frozenset()
    version: str = "1"
    risk_level: str = "write"
    idempotent: bool = False
    name: str
    description: str
    input_schema: dict[str, object]
    params_model: ClassVar[type[BaseModel] | None] = None
    # 声明可安全自动重试的 error_type；默认仅 rate_limited（语义上"请求未执行"）
    # runtime_error 可能发生在副作用已部分生效之后，须由工具显式声明才允许重试
    retryable_errors: frozenset[str] = frozenset({"rate_limited"})

    # 将实现、服务端、参数模式和能力绑定成稳定身份，元数据变化立即撤销授权
    def security_identity(self) -> str:
        return digest(
            {
                "implementation": f"{type(self).__module__}.{type(self).__qualname__}",
                "name": self.name,
                "server": self.server_id,
                "version": self.version,
                "schema": self.input_schema,
                "capabilities": sorted(self.capabilities),
            }
        )

    # 工具自检仅能增加限制，不能返回 ALLOW 跳过宿主安全门
    def check_permissions(self, context: SecurityContext, params: dict[str, object]) -> str | None:
        root = getattr(self, "_root", None)
        if root is not None and root.resolve() != context.project_root.resolve():
            return "tool_workspace_mismatch"
        return None

    # 执行工具调用，返回结果或错误
    @abstractmethod
    async def invoke(self, params: dict[str, object]) -> ToolResult: ...
