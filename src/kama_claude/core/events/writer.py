from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import IO

from pydantic import BaseModel

from kama_claude.core.events.bus import EventBus
from kama_claude.core.security.redaction import redact

logger = logging.getLogger(__name__)


class EventWriter:
    def __init__(self, path: Path, run_id: str | None = None) -> None:
        self._path = path
        self._run_id = run_id
        self._unsubscribe: Callable[[], None] | None = None
        self._cleanups: list[Callable[[], None]] = []
        self._file: IO[str] | None = None

    # 打开事件文件（追加模式），供 async with 使用
    async def __aenter__(self) -> EventWriter:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self._path, "a", encoding="utf-8")
        self._path.chmod(0o600)
        return self

    # 关闭事件文件
    async def __aexit__(self, *args: object) -> None:
        for cleanup in self._cleanups:
            cleanup()
        self._cleanups.clear()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._file is not None:
            self._file.close()
            self._file = None

    # 将事件序列化为 JSON 行并写入文件，写入失败时记录日志但不抛出异常
    async def handle(self, event: BaseModel) -> None:
        if self._file is None:
            return
        if self._run_id is not None and getattr(event, "run_id", None) != self._run_id:
            return
        try:
            import json

            self._file.write(
                json.dumps(
                    redact(
                        {**event.model_dump(), "_sequence": getattr(event, "_event_sequence", 0)}
                    ),
                    ensure_ascii=False,
                )
                + "\n"
            )
            self._file.flush()
        except (OSError, ValueError) as e:
            logger.error("EventWriter: failed to write event: %s", e)

    # 登记与此运行绑定的额外清理函数，异常路径也会执行
    def on_close(self, cleanup: Callable[[], None]) -> None:
        self._cleanups.append(cleanup)

    # 将 handle 注册为 bus 的订阅者
    def subscribe(self, bus: EventBus) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
        self._unsubscribe = bus.subscribe(self.handle)
