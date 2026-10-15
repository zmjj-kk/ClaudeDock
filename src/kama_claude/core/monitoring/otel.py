"""OpenTelemetry 集成：导出指标到 OTLP 端点（Prometheus/Grafana）。

环境变量配置：
- OTEL_EXPORTER_OTLP_ENDPOINT: OTLP 端点（例如 http://localhost:4318）
- OTEL_SERVICE_NAME: 服务名称（默认 "kama-core"）
- OTEL_ENABLED: 是否启用（默认 false）
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_shutdown: list[Any] = []


# 检查 OpenTelemetry 是否可用
def is_otel_available() -> bool:
    """检查是否安装了 opentelemetry-sdk"""
    try:
        import opentelemetry  # noqa: F401

        return True
    except ImportError:
        return False


# 初始化 OpenTelemetry（如果启用且可用）
def setup_otel() -> None:
    """设置 OpenTelemetry 追踪和指标导出器"""
    if not os.getenv("OTEL_ENABLED", "false").lower() == "true":
        logger.debug("OpenTelemetry disabled (OTEL_ENABLED not set)")
        return

    if not is_otel_available():
        logger.warning(
            "OpenTelemetry enabled but SDK not installed. "
            "Install with: uv add opentelemetry-sdk opentelemetry-exporter-otlp"
        )
        return

    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        service_name = os.getenv("OTEL_SERVICE_NAME", "kama-core")
        resource = Resource(attributes={"service.name": service_name})

        # 配置 Trace Provider
        trace_provider = TracerProvider(resource=resource)
        trace_exporter = OTLPSpanExporter()
        trace_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
        trace.set_tracer_provider(trace_provider)
        _shutdown.append(trace_provider.shutdown)

        # 配置 Metrics Provider
        metric_exporter = OTLPMetricExporter()
        metric_reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=60000)
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(meter_provider)
        _shutdown.append(meter_provider.shutdown)

        logger.info(
            "OpenTelemetry initialized",
            extra={
                "service_name": service_name,
                "endpoint": os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "default"),
            },
        )
    except Exception:
        logger.exception("Failed to initialize OpenTelemetry")


# 缓存指标器具，避免每次观测重新创建同名 Histogram
_instruments: dict[tuple[str, str], Any] = {}


# 将每个原始观测值直接写入 OTel，禁止用聚合 P50 冒充样本
def record_observation(name: str, value: float, unit: str, tags: dict[str, str]) -> None:
    if os.getenv("OTEL_ENABLED", "false").lower() != "true":
        return
    try:
        from opentelemetry import metrics

        key = (name, unit)
        instrument = _instruments.get(key)
        if instrument is None:
            if len(_instruments) >= 256:
                return
            meter = metrics.get_meter(__name__)
            instrument = (
                meter.create_histogram(f"kama.{name}.duration", unit=unit)
                if unit == "ms"
                else meter.create_counter(f"kama.{name}")
            )
            _instruments[key] = instrument
        if unit == "ms":
            instrument.record(value, attributes=tags)
        else:
            instrument.add(value, attributes=tags)
    except Exception:
        logger.debug("OTel observation unavailable", exc_info=True)


# 兼容旧摘要出口，聚合摘要只能作为日志，不重新计入原始观测
def export_metrics_to_otel(metrics_summary: dict[str, Any]) -> None:
    logger.debug("Aggregated metrics summary: %s", metrics_summary)


# 守护进程关闭时刷新并停止所有已初始化的 OTel 导出器
def shutdown_otel() -> None:
    for shutdown in reversed(_shutdown):
        shutdown()
    _shutdown.clear()
