from __future__ import annotations

from typing import Any

from kama_claude.core.events.bus import EventBus
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.tools.invocation import invoke_tool as production_invoke


# 在执行器单测中模拟真实人工批准，仍经过完整安全网关和 token 校验
def approving_manager(bus: EventBus) -> PermissionManager:
    manager = PermissionManager(timeout_s=0.1)

    # 回传收到的原始一次性 token，避免测试依赖缺失审批的旧行为
    async def approve(event: Any) -> None:
        if getattr(event, "type", "") == "permission.requested":
            manager.respond(
                event.tool_use_id,
                "allow_once",
                approval_token=event.approval_token,
            )

    bus.subscribe(approve)
    return manager


# 仅用于测试执行、超时与重试行为，未知工具也必须先经模拟人工审批
async def invoke_tool(*args: Any, **kwargs: Any) -> Any:
    bus = args[2] if len(args) > 2 else kwargs["bus"]
    if kwargs.get("permission_manager") is None:
        kwargs["permission_manager"] = approving_manager(bus)
    return await production_invoke(*args, **kwargs)
