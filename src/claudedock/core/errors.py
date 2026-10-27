"""ClaudeDock 异常体系：分层错误类型，便于精准处理和调试。"""

from __future__ import annotations


# ── 基础异常 ──
class ClaudeDockError(Exception):
    """所有 ClaudeDock 异常的基类"""

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


# ── 配置与初始化错误 ──
class ConfigurationError(ClaudeDockError):
    """配置文件解析失败、必需配置缺失、配置值非法"""


class InitializationError(ClaudeDockError):
    """系统组件初始化失败（端口占用、依赖服务不可达等）"""


# ── LLM 相关错误 ──
class LLMError(ClaudeDockError):
    """LLM 调用相关错误的基类"""


class LLMConnectionError(LLMError):
    """无法连接到 LLM 服务（网络超时、DNS 解析失败）"""


class LLMAuthenticationError(LLMError):
    """API key 无效或已过期"""


class LLMRateLimitError(LLMError):
    """触发 LLM 服务速率限制"""


class LLMResponseError(LLMError):
    """LLM 响应格式非法或解析失败"""


# ── 工具执行错误 ──
class ToolError(ClaudeDockError):
    """工具调用相关错误的基类"""


class ToolNotFoundError(ToolError):
    """请求的工具未注册"""

    def __init__(self, tool_name: str) -> None:
        super().__init__(f"Tool not found: {tool_name}", details={"tool": tool_name})
        self.tool_name = tool_name


class ToolExecutionError(ToolError):
    """工具执行过程中抛出异常"""

    def __init__(
        self,
        tool_name: str,
        original_error: Exception,
        *,
        tool_input: dict[str, object] | None = None,
    ) -> None:
        super().__init__(
            f"Tool execution failed: {tool_name}",
            details={"tool": tool_name, "input": tool_input, "error": str(original_error)},
        )
        self.tool_name = tool_name
        self.original_error = original_error
        self.tool_input = tool_input


class ToolValidationError(ToolError):
    """工具输入参数校验失败"""


class ToolTimeoutError(ToolError):
    """工具执行超时"""


# ── 权限与安全错误 ──
class PermissionError(ClaudeDockError):
    """权限相关错误的基类"""


class PermissionDeniedError(PermissionError):
    """用户拒绝或策略禁止工具调用"""

    def __init__(self, tool_name: str, reason: str = "") -> None:
        super().__init__(
            f"Permission denied: {tool_name}" + (f" ({reason})" if reason else ""),
            details={"tool": tool_name, "reason": reason},
        )
        self.tool_name = tool_name
        self.reason = reason


class PermissionTimeoutError(PermissionError):
    """等待用户审批超时"""


# ── 会话与上下文错误 ──
class SessionError(ClaudeDockError):
    """会话相关错误的基类"""


class SessionNotFoundError(SessionError):
    """请求的会话 ID 不存在"""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"Session not found: {session_id}", details={"session_id": session_id})
        self.session_id = session_id


class SessionClosedError(SessionError):
    """尝试操作已关闭的会话"""


class ContextWindowExceededError(SessionError):
    """上下文窗口超出限制且无法压缩"""


# ── 存储与持久化错误 ──
class StorageError(ClaudeDockError):
    """存储相关错误的基类"""


class StorageConnectionError(StorageError):
    """无法连接到存储后端（Qdrant、SQLite 等）"""


class StorageWriteError(StorageError):
    """写入存储失败"""


class StorageReadError(StorageError):
    """从存储读取失败"""


# ── 协议与传输错误 ──
class ProtocolError(ClaudeDockError):
    """协议层错误的基类"""


class InvalidRequestError(ProtocolError):
    """JSON-RPC 请求格式非法"""


class MethodNotFoundError(ProtocolError):
    """请求的 RPC 方法不存在"""

    def __init__(self, method: str) -> None:
        super().__init__(f"Method not found: {method}", details={"method": method})
        self.method = method


class TransportError(ProtocolError):
    """传输层错误（连接断开、socket 异常）"""


# 对外导出的异常类集合；旧代码仍可通过 claudedock.core.permissions 获取 PermissionDeniedError
__all__ = [
    "ClaudeDockError",
    "ConfigurationError",
    "InitializationError",
    "LLMError",
    "LLMConnectionError",
    "LLMAuthenticationError",
    "LLMRateLimitError",
    "LLMResponseError",
    "ToolError",
    "ToolNotFoundError",
    "ToolExecutionError",
    "ToolValidationError",
    "ToolTimeoutError",
    "PermissionError",
    "PermissionDeniedError",
    "PermissionTimeoutError",
    "SessionError",
    "SessionNotFoundError",
    "SessionClosedError",
    "ContextWindowExceededError",
    "StorageError",
    "StorageConnectionError",
    "StorageWriteError",
    "StorageReadError",
    "ProtocolError",
    "InvalidRequestError",
    "MethodNotFoundError",
    "TransportError",
]
