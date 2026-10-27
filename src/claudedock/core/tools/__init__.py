from claudedock.core.tools.base import BaseTool, ToolResult
from claudedock.core.tools.registry import ToolRegistry

__all__ = ["BaseTool", "ToolResult", "ToolRegistry", "invoke_tool"]


# 延迟导入调用器，避免工具结果存储经包初始化形成循环依赖
def __getattr__(name: str) -> object:
    if name == "invoke_tool":
        from claudedock.core.tools.invocation import invoke_tool

        return invoke_tool
    raise AttributeError(name)
