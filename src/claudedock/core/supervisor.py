from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T")
log = logging.getLogger(__name__)


class RunSupervisor:
    # 限制运行与后台作业数量，并统一持有任务以传播关闭取消
    def __init__(self, max_runs: int = 8, max_jobs: int = 32) -> None:
        self._slots = asyncio.Semaphore(max_runs)
        self._max_jobs = max_jobs
        self._tasks: set[asyncio.Task[Any]] = set()
        self._keys: dict[str, asyncio.Task[Any]] = {}
        self._closing = False
        self._runs: dict[str, asyncio.Task[Any]] = {}
        self._states: dict[str, str] = {}

    # 在有限并发槽内执行一次运行，调用方取消会传播至任务
    async def run(self, work: Coroutine[Any, Any, T], *, run_id: str | None = None) -> T:
        if self._closing or (
            len(self._tasks) >= self._max_jobs and asyncio.current_task() not in self._tasks
        ):
            work.close()
            raise RuntimeError("daemon is draining")
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        if run_id is not None:
            self._runs[run_id] = task
            self._states[run_id] = "queued"
        try:
            async with self._slots:
                if run_id is not None:
                    self._states[run_id] = "running"
                result = await work
                if run_id is not None and self._states.get(run_id) == "running":
                    self._states[run_id] = "completed"
                return result
        except asyncio.CancelledError:
            if run_id is not None:
                self._states[run_id] = "interrupted"
            raise
        except Exception:
            if run_id is not None:
                self._states[run_id] = "failed"
            raise
        finally:
            work.close()
            self._tasks.discard(task)
            for old_id in list(self._runs):
                if len(self._runs) <= 512:
                    break
                if self._runs[old_id].done():
                    self._runs.pop(old_id, None)
                    self._states.pop(old_id, None)

    # 从事件更新审批等待和真实最终状态，不将失败误报为完成
    async def observe(self, event: BaseModel) -> None:
        run_id = getattr(event, "run_id", None)
        if run_id not in self._states:
            return
        kind = getattr(event, "type", "")
        if kind == "permission.requested":
            self._states[run_id] = "waiting_approval"
        elif kind in {"permission.granted", "permission.denied"}:
            self._states[run_id] = "running"
        elif kind == "run.finished":
            self._states[run_id] = (
                "completed" if getattr(event, "status", "") == "success" else "failed"
            )

    # 返回有界内存中的运行状态，不存在时由持久化检查点查询
    def state(self, run_id: str) -> str | None:
        return self._states.get(run_id)

    # 取消指定运行，后代回收由 Runner 的取消路径负责
    def cancel(self, run_id: str) -> bool:
        task = self._runs.get(run_id)
        if task is None or task.done():
            return False
        self._states[run_id] = "cancelling"
        task.cancel()
        return True

    # 注册有界后台作业，同一键尚未完成时跳过重复作业
    def spawn(self, work: Coroutine[Any, Any, T], *, key: str | None = None) -> asyncio.Task[T]:
        if key is not None and key in self._keys and not self._keys[key].done():
            work.close()
            return self._keys[key]
        if self._closing or len(self._tasks) >= self._max_jobs:
            work.close()
            raise RuntimeError("background job capacity exhausted")
        task = asyncio.create_task(work)
        self._tasks.add(task)
        if key is not None:
            self._keys[key] = task

        # 完成后回收任务并读取异常，避免后台失败无人处理
        def finished(done: asyncio.Task[T]) -> None:
            self._tasks.discard(done)
            if key is not None and self._keys.get(key) is done:
                self._keys.pop(key, None)
            if not done.cancelled() and done.exception() is not None:
                log.error("supervised job failed: %s", done.exception())

        task.add_done_callback(finished)
        return task

    # 停止接收新任务并取消、等待全部托管任务
    async def aclose(self) -> None:
        self._closing = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._keys.clear()
        self._runs.clear()
