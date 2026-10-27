from __future__ import annotations

import asyncio

from claudedock.core.context import ExecutionContext


# 管理后台 subagent 任务的生命周期：注册、查询、批量取消
class BackgroundTaskRegistry:
    def __init__(self, *, limit: int = 128) -> None:
        self._tasks: dict[str, tuple[asyncio.Task[None], ExecutionContext]] = {}
        self._limit = limit

    # 注册一个后台任务及其执行上下文
    def register(
        self,
        run_id: str,
        task: asyncio.Task[None],
        context: ExecutionContext,
    ) -> None:
        for old_id, (old_task, _) in list(self._tasks.items()):
            if len(self._tasks) < self._limit:
                break
            if old_task.done():
                self._tasks.pop(old_id, None)
        if len(self._tasks) >= self._limit:
            task.cancel()
            raise RuntimeError("background agent capacity exhausted")
        self._tasks[run_id] = (task, context)

        # 完成时读取异常，保留有界结果缓存供后续运行查询
        def finished(done: asyncio.Task[None]) -> None:
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)

    # 守护进程关闭时取消并等待全部后台子任务
    async def aclose(self) -> None:
        tasks = [task for task, _ in self._tasks.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    # 递归取消指定父运行的全部后台后代并等待回收
    async def cancel_children(self, parent_run_id: str) -> None:
        parents = {parent_run_id}
        selected: list[asyncio.Task[None]] = []
        changed = True
        while changed:
            changed = False
            for run_id, (task, context) in list(self._tasks.items()):
                if context.parent_run_id in parents and run_id not in parents:
                    parents.add(run_id)
                    task.cancel()
                    selected.append(task)
                    changed = True
        if selected:
            await asyncio.gather(*selected, return_exceptions=True)

    # 查询后台任务及其上下文；不存在时返回 None
    def get(self, run_id: str) -> tuple[asyncio.Task[None], ExecutionContext] | None:
        return self._tasks.get(run_id)

    # 返回所有已注册的 (task, context) 对，用于 daemon 退出时批量清理
    def all(self) -> list[tuple[asyncio.Task[None], ExecutionContext]]:
        return list(self._tasks.values())

    # 移除指定 run_id 的后台任务登记（不取消运行中的任务）；不存在时静默返回
    def remove(self, run_id: str) -> None:
        self._tasks.pop(run_id, None)

    # 取消并移除指定 run_id 的后台任务；不存在时静默返回
    def cancel(self, run_id: str) -> None:
        entry = self._tasks.pop(run_id, None)
        if entry is not None:
            task, _ = entry
            task.cancel()
