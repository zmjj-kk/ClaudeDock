from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

from kama_claude.core.config import KamaConfig
from kama_claude.core.security.redaction import redact

_TEXT_FMT = 'level=%(levelname)s ts=%(asctime)s source=%(name)s msg="%(message)s"'


# 结构化日志上下文提取器：从日志消息中提取 key=value 对
class StructuredFormatter(logging.Formatter):
    """支持结构化日志的 JSON 格式化器，自动提取上下文字段"""

    # 匹配 key=value 格式（支持引号包裹的值）
    _KV_PATTERN = re.compile(r'(\w+)=(?:"([^"]*)"|([^\s]*))')

    def format(self, record: logging.LogRecord) -> str:
        # 基础字段
        log_data: dict[str, Any] = {
            "level": record.levelname,
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "source": record.name,
            "msg": record.getMessage(),
        }

        # 提取结构化字段（run_id、session_id、step 等）
        if hasattr(record, "run_id"):
            log_data["run_id"] = record.run_id
        if hasattr(record, "session_id"):
            log_data["session_id"] = record.session_id
        if hasattr(record, "step"):
            log_data["step"] = record.step

        # 从消息中提取 key=value 对
        matches = self._KV_PATTERN.findall(record.getMessage())
        for key, quoted_val, unquoted_val in matches:
            if key not in log_data:
                log_data[key] = quoted_val or unquoted_val

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        # 敏感信息脱敏
        log_data = self._sanitize(log_data)

        return json.dumps(redact(log_data), ensure_ascii=False)

    def _sanitize(self, data: dict[str, Any]) -> dict[str, Any]:
        """脱敏敏感字段（API key、文件路径）"""
        sanitized = data.copy()

        # API key 脱敏：sk-ant-xxx... → sk-ant-***
        if "api_key" in sanitized:
            key = str(sanitized["api_key"])
            if key.startswith("sk-ant-"):
                sanitized["api_key"] = "sk-ant-***"

        # 文件路径脱敏：仅保留文件名
        if "file_path" in sanitized:
            path = str(sanitized["file_path"])
            sanitized["file_path"] = Path(path).name

        return sanitized


class RedactingFormatter(logging.Formatter):
    # 对格式化后的文本与异常栈统一脱敏
    def format(self, record: logging.LogRecord) -> str:
        return str(redact(super().format(record)))


# 根据配置初始化 root logger：设置级别、格式，并挂载 stderr 和可选的滚动文件 handler
def setup_logging(config: KamaConfig) -> None:
    level = getattr(logging, config.logging.level.upper(), logging.INFO)

    # 选择格式化器：JSON 格式使用结构化提取器，文本格式使用简单模板
    if config.logging.format == "json":
        formatter: logging.Formatter = StructuredFormatter()
    else:
        formatter = RedactingFormatter(_TEXT_FMT, datefmt="%Y-%m-%dT%H:%M:%S")

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    root.addHandler(stderr_handler)

    if config.logging.file:
        log_path = Path(config.logging.file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


# 创建带上下文的日志记录器适配器
class ContextLogger(logging.LoggerAdapter[Any]):
    """为日志添加结构化上下文（run_id、session_id、step）"""

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        # 将上下文字段注入到 LogRecord extra 中
        extra = kwargs.get("extra", {})
        extra.update(self.extra)
        kwargs["extra"] = extra
        return msg, kwargs


# 工厂函数：创建带上下文的 logger
def get_logger(
    name: str,
    *,
    run_id: str | None = None,
    session_id: str | None = None,
    step: int | None = None,
) -> logging.LoggerAdapter[Any]:
    """创建带结构化上下文的日志记录器"""
    logger = logging.getLogger(name)
    context: dict[str, Any] = {}
    if run_id:
        context["run_id"] = run_id
    if session_id:
        context["session_id"] = session_id
    if step is not None:
        context["step"] = step
    return ContextLogger(logger, context)
