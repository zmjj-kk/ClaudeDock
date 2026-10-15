"""性能监控与追踪：记录关键路径的延迟指标，支持 OpenTelemetry 导出。

核心指标：
- LLM 调用延迟（P50/P95/P99）
- 工具执行时间
- 内存/向量检索耗时
- RAG 查询端到端延迟
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any

from kama_claude.core.monitoring.otel import record_observation

logger = logging.getLogger(__name__)


@dataclass
class Metric:
    """单个性能指标快照"""

    name: str
    value: float
    unit: str  # "ms", "s", "bytes"
    tags: dict[str, str] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class LatencyStats:
    """延迟统计：P50/P95/P99/max/count"""

    p50: float
    p95: float
    p99: float
    max: float
    count: int


class MetricsCollector:
    """性能指标收集器：支持计时器、计数器、直方图"""

    def __init__(self) -> None:
        # 计时器：operation_name → [duration_ms, ...]
        self._timings: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=4096))
        # 计数器：metric_name → count
        self._counters: dict[str, int] = defaultdict(int)
        # 原始指标流
        self._metrics: deque[Metric] = deque(maxlen=8192)

    # 记录一次计时（毫秒）
    def record_timing(
        self, operation: str, duration_ms: float, tags: dict[str, str] | None = None
    ) -> None:
        if operation not in self._timings and len(self._timings) >= 256:
            return
        record_observation(operation, duration_ms, "ms", tags or {})
        self._timings[operation].append(duration_ms)
        self._metrics.append(Metric(name=operation, value=duration_ms, unit="ms", tags=tags or {}))

    # 增加计数器
    def increment(self, metric: str, delta: int = 1, tags: dict[str, str] | None = None) -> None:
        key = f"{metric}:{','.join(f'{k}={v}' for k, v in sorted((tags or {}).items()))}"
        record_observation(metric, delta, "count", tags or {})
        if key not in self._counters and len(self._counters) >= 256:
            return
        self._counters[key] += delta
        self._metrics.append(Metric(name=metric, value=delta, unit="count", tags=tags or {}))

    # 获取延迟统计（百分位数）
    def get_latency_stats(self, operation: str) -> LatencyStats | None:
        timings = sorted(self._timings.get(operation, []))
        if not timings:
            return None

        count = len(timings)
        return LatencyStats(
            p50=self._percentile(timings, 0.50),
            p95=self._percentile(timings, 0.95),
            p99=self._percentile(timings, 0.99),
            max=timings[-1],
            count=count,
        )

    # 导出所有原始指标（供 OpenTelemetry 消费）
    def export_metrics(self) -> list[Metric]:
        return list(self._metrics)

    # 清空所有指标
    def reset(self) -> None:
        self._timings.clear()
        self._counters.clear()
        self._metrics.clear()

    # 生成摘要报告（人类可读）
    def summary(self) -> dict[str, Any]:
        result: dict[str, Any] = {"timings": {}, "counters": dict(self._counters)}

        for operation, timings in self._timings.items():
            stats = self.get_latency_stats(operation)
            if stats:
                result["timings"][operation] = {
                    "p50_ms": round(stats.p50, 2),
                    "p95_ms": round(stats.p95, 2),
                    "p99_ms": round(stats.p99, 2),
                    "max_ms": round(stats.max, 2),
                    "count": stats.count,
                }

        return result

    @staticmethod
    def _percentile(sorted_values: list[float], p: float) -> float:
        """计算百分位数（线性插值）"""
        if not sorted_values:
            return 0.0
        idx = p * (len(sorted_values) - 1)
        lower = int(idx)
        upper = min(lower + 1, len(sorted_values) - 1)
        weight = idx - lower
        return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


# 全局指标收集器实例
_collector = MetricsCollector()


# 获取全局收集器
def get_collector() -> MetricsCollector:
    return _collector


# 同步计时上下文管理器
@contextmanager
def timed(operation: str, tags: dict[str, str] | None = None) -> Iterator[None]:
    """记录代码块执行时间

    Example:
        with timed("llm.chat", {"model": "claude-3-5-sonnet"}):
            response = await client.chat(...)
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        _collector.record_timing(operation, duration_ms, tags)


# 异步计时上下文管理器
@asynccontextmanager
async def async_timed(operation: str, tags: dict[str, str] | None = None) -> AsyncIterator[None]:
    """记录异步代码块执行时间

    Example:
        async with async_timed("rag.search", {"query_type": "vector"}):
            results = await rag_store.search(query)
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        _collector.record_timing(operation, duration_ms, tags)


# 日志性能报告（每 N 次调用或定期触发）
def log_summary() -> None:
    """将指标摘要输出到日志"""
    summary = _collector.summary()
    if summary["timings"] or summary["counters"]:
        logger.info("Performance metrics summary", extra={"metrics": summary})
