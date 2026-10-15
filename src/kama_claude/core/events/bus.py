from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from pydantic import BaseModel

type EventHandler = Callable[[BaseModel], Awaitable[None]]

logger = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[EventHandler] = []
        self._sequences: dict[str, int] = {}
        self._active_runs: set[str] = set()

    # 注册一个事件处理函数
    def subscribe(self, handler: EventHandler) -> Callable[[], None]:
        self._subscribers.append(handler)

        # 幂等释放此次订阅，允许发布过程中安全移除监听者
        def unsubscribe() -> None:
            if handler in self._subscribers:
                self._subscribers.remove(handler)

        return unsubscribe

    # 返回活动订阅数量，供稳定性检查与监控使用
    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # 按注册顺序依次调用所有订阅者；单个订阅者异常被隔离，不阻断后续订阅者也传播到调用方
    async def publish(self, event: BaseModel) -> None:
        run_id = getattr(event, "run_id", None)
        if run_id is not None:
            kind = getattr(event, "type", "")
            if kind in {"run.started", "subagent.started"}:
                self._active_runs.add(run_id)
            elif kind in {"run.finished", "subagent.finished"}:
                self._active_runs.discard(run_id)
            sequence = self._sequences.get(run_id, 0) + 1
            self._sequences[run_id] = sequence
            setattr(event, "_event_sequence", sequence)
            if len(self._sequences) > 512:
                oldest = next(
                    (key for key in self._sequences if key not in self._active_runs), None
                )
                if oldest is not None:
                    self._sequences.pop(oldest)
        for handler in list(self._subscribers):
            try:
                await handler(event)
            except Exception:
                logger.exception(
                    "event handler failed type=%s", getattr(event, "type", "<unknown>")
                )
