from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from kama_claude.core.security.context import digest


@dataclass(frozen=True)
class PreparedEffect:
    operation_id: str
    principal_id: str
    argument_hash: str
    impact_hash: str
    expires_at: float
    backend_token: str


class BusinessBackend(Protocol):
    # 后端必须重新鉴权并将影响摘要、资源归属和审批要求绑定至服务端 token
    async def dry_run(
        self,
        principal_id: str,
        operation_id: str,
        arguments: dict[str, object],
    ) -> PreparedEffect: ...

    # 后端持久化幂等状态；重复操作必须返回已有状态而不是再次写入
    async def status(self, principal_id: str, operation_id: str) -> str: ...

    # 后端验证 token、审批、参数摘要和有效期并原子消费操作 ID
    async def execute(
        self,
        effect: PreparedEffect,
        arguments: dict[str, object],
        approval: str,
    ) -> str: ...


class EffectExecutor:
    # 只接受受信业务后端；本地 journal 不能充当远端写入幂等账本
    def __init__(self, backend: BusinessBackend) -> None:
        self.backend = backend

    # 已发出或结果未知的操作只查询状态，禁止自动重发；真正幂等由后端原子保证
    async def execute(
        self,
        effect: PreparedEffect,
        arguments: dict[str, object],
        principal_id: str,
        approval: str,
    ) -> str:
        if (
            effect.principal_id != principal_id
            or effect.argument_hash != digest(arguments)
            or effect.expires_at <= time.time()
            or not effect.backend_token
            or not approval
        ):
            raise PermissionError("business approval scope invalid")
        state = await self.backend.status(principal_id, effect.operation_id)
        if state != "prepared":
            return state
        try:
            return await self.backend.execute(effect, arguments, approval)
        except TimeoutError:
            return await self.backend.status(principal_id, effect.operation_id)
