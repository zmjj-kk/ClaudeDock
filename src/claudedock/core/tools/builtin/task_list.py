from __future__ import annotations

from claudedock.core.security.capabilities import Capability
from claudedock.core.task.manager import TaskManager
from claudedock.core.tools.base import BaseTool, ToolResult


class TaskListTool(BaseTool):
    capabilities = frozenset({Capability.READ})
    name = "task_list"
    description = (
        "List all tasks with their current status and blocking dependencies. "
        "Use this to check what work remains and what can be started next."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    # 只读工具：runtime_error 可安全重试，不会产生副作用
    retryable_errors = frozenset({"rate_limited", "runtime_error"})

    # 持有 TaskManager 实例，供 invoke 调用
    def __init__(self, task_manager: TaskManager) -> None:
        self._manager = task_manager

    # 返回格式化的任务列表摘要
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult(content=self._manager.format_list())
