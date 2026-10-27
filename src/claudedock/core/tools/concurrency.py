from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from claudedock.core.tools.paths import resolve_inside

ConcurrencyPolicy = Literal["parallel_read", "resource_scoped", "serialized"]


@dataclass
class ExecutionStats:
    active: int = 0
    peak: int = 0


current_execution_stats: ContextVar[ExecutionStats | None] = ContextVar(
    "tool_execution_stats",
    default=None,
)


# 仅统计已取得全部执行槽的实际工具调用，不将审批与排队计入峰值
@contextmanager
def track_execution() -> Iterator[None]:
    stats = current_execution_stats.get()
    if stats is not None:
        stats.active += 1
        stats.peak = max(stats.peak, stats.active)
    try:
        yield
    finally:
        if stats is not None:
            stats.active -= 1


@dataclass(frozen=True)
class ResourceClaim:
    resource: Path
    mode: Literal["read", "write"]

    # 目录覆盖、父子路径及同一路径均保守视为重叠
    def conflicts(self, other: ResourceClaim) -> bool:
        overlap = (
            self.resource == other.resource
            or self.resource in other.resource.parents
            or other.resource in self.resource.parents
        )
        return overlap and (self.mode == "write" or other.mode == "write")


# 仅从宿主工具根目录与已校验参数推导文件资源，路径拒绝则退化为屏障
def file_claim(root: Path, value: object, mode: Literal["read", "write"]) -> ResourceClaim | None:
    if not isinstance(value, str):
        return None
    safe = resolve_inside(root, value)
    return ResourceClaim(safe.path, mode) if safe.path is not None else None


# 将有界文件操作移出事件循环，取消时等待线程回收后才释放执行槽
async def run_file_io[T](function: Callable[[], T]) -> T:
    worker = asyncio.create_task(asyncio.to_thread(function))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise
