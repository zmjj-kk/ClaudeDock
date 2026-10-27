from __future__ import annotations

import asyncio
import logging
import os
from io import BufferedWriter, FileIO, TextIOWrapper
from logging.handlers import RotatingFileHandler
from pathlib import Path

from claudedock.core.security.redaction import redact
from claudedock.core.trace.record import TraceRecord


class _PrivateRotatingHandler(RotatingFileHandler):
    # 每个新日志文件以 0600 打开，并拒绝符号链接
    def _open(self) -> TextIOWrapper[BufferedWriter]:
        fd = os.open(
            self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
        )
        return TextIOWrapper(BufferedWriter(FileIO(fd, mode="a", closefd=True)), encoding="utf-8")


class TraceWriter:
    # 初始化 TraceWriter；写入目标文件路径在 start() 前不会创建
    def __init__(
        self,
        path: Path,
        *,
        include_payload: bool = False,
        max_bytes: int = 10 * 1024 * 1024,
        backups: int = 3,
    ) -> None:
        self._max_bytes = max_bytes
        self._backups = backups
        self._include_payload = include_payload
        self._path = path
        self._queue: asyncio.Queue[TraceRecord] = asyncio.Queue(maxsize=2048)
        self.dropped = 0
        self._task: asyncio.Task[None] | None = None

    # 创建目录、启动后台 drain task
    async def start(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._drain())

    # 等待队列清空后取消 drain task
    async def stop(self) -> None:
        if self._task is None:
            return
        if self._task.done():
            await self._task
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout=5.0)
        except TimeoutError:
            pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # 非阻塞地将 record 放入写入队列
    def emit(self, record: TraceRecord) -> None:
        try:
            self._queue.put_nowait(record.model_copy(update={"data": self._safe_data(record.data)}))
        except asyncio.QueueFull:
            self.dropped += 1

    # 默认移除请求、响应与事件正文，仅保留关联元数据和统计
    def _safe_data(self, data: dict[str, object]) -> dict[str, object]:
        payloads = {
            "messages",
            "text",
            "token",
            "goal",
            "content",
            "input",
            "params",
            "output",
            "result",
            "thinking",
            "error_message",
            "param_preview",
        }
        if self._include_payload:
            return dict(redact(data))

        # 递归过滤事件与响应中的正文，详细模式也始终先脱敏
        def strip(value: object) -> object:
            if isinstance(value, dict):
                return {k: strip(v) for k, v in value.items() if k not in payloads}
            if isinstance(value, list):
                return [strip(v) for v in value]
            return value

        return dict(redact(strip(data)))

    # 持续从队列读取 record 并追加写入文件
    async def _drain(self) -> None:
        handler = _PrivateRotatingHandler(
            self._path, maxBytes=self._max_bytes, backupCount=self._backups, encoding="utf-8"
        )
        self._path.chmod(0o600)
        try:
            while True:
                record = await self._queue.get()
                try:
                    entry = logging.LogRecord(
                        "trace", logging.INFO, "", 0, record.model_dump_json(), (), None
                    )
                    if handler.shouldRollover(entry):
                        handler.doRollover()
                    assert handler.stream is not None
                    handler.stream.write(record.model_dump_json() + "\n")
                    handler.flush()
                finally:
                    self._queue.task_done()
        finally:
            handler.close()
