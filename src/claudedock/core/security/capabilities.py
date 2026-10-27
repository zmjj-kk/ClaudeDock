from __future__ import annotations

from enum import StrEnum


class Capability(StrEnum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    NETWORK = "network"
    EXTERNAL_EFFECT = "external_effect"


# 内置只读快速路径由宿主代码授权，禁止依赖 MCP 名称或自报 readOnlyHint
READ_TOOLS = frozenset(
    {
        "read_file",
        "list_dir",
        "task_get",
        "task_list",
        "memory_search",
        "memory_query",
        "tool_search",
        "read_tool_result",
        "use_skill",
        "read_skill_resource",
    }
)
