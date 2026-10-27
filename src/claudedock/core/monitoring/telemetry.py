from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from typing import Any

from claudedock.core.security.redaction import redact


# 为真实操作建立 Span 并记录原始耗时，关联键仅用于 Trace 而不扩张指标维度
@contextmanager
def observe(name: str, attributes: dict[str, Any]) -> Iterator[None]:
    manager: Any = nullcontext()
    if os.getenv("OTEL_ENABLED", "false").lower() == "true":
        try:
            from opentelemetry import trace

            manager = trace.get_tracer("claudedock").start_as_current_span(
                name,
                attributes=redact(attributes),
                record_exception=False,
                set_status_on_exception=False,
            )
        except ImportError:
            pass
    started = time.perf_counter()
    outcome = "success"
    try:
        with manager as span:
            try:
                yield
                outcome = str(attributes.get("outcome", outcome))
                if span is not None:
                    span.set_attribute("outcome", outcome)
            except BaseException:
                outcome = "failed"
                if span is not None:
                    span.set_attribute("outcome", outcome)
                raise
    finally:
        from claudedock.core.monitoring.metrics import get_collector

        tags = {k: str(v) for k, v in attributes.items() if k in {"tool_name", "model", "method"}}
        tags["outcome"] = outcome
        get_collector().record_timing(name, (time.perf_counter() - started) * 1000, tags)
