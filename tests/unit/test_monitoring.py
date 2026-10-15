"""测试性能监控模块：指标收集、计时器、延迟统计。"""

from __future__ import annotations

import asyncio
import time

from kama_claude.core.monitoring import (
    MetricsCollector,
    async_timed,
    get_collector,
    timed,
)


# 功能：验证 MetricsCollector 记录计时器数据
# 设计：手动记录多次计时，验证数据被正确存储
def test_metrics_collector_records_timing() -> None:
    collector = MetricsCollector()
    collector.record_timing("test_op", 100.5)
    collector.record_timing("test_op", 200.3)

    stats = collector.get_latency_stats("test_op")
    assert stats is not None
    assert stats.count == 2
    assert stats.max > 200


# 功能：验证延迟统计计算正确的百分位数
# 设计：记录已知分布的数据，断言 P50/P95/P99 计算正确
def test_latency_stats_percentiles() -> None:
    collector = MetricsCollector()
    # 记录 100 个均匀分布的值：1, 2, 3, ..., 100
    for i in range(1, 101):
        collector.record_timing("uniform", float(i))

    stats = collector.get_latency_stats("uniform")
    assert stats is not None
    assert 49 <= stats.p50 <= 51  # P50 应接近 50
    assert 94 <= stats.p95 <= 96  # P95 应接近 95
    assert 98 <= stats.p99 <= 100  # P99 应接近 99
    assert stats.max == 100.0


# 功能：验证计数器正确累加
# 设计：多次增加计数器，验证最终值正确
def test_metrics_collector_counter() -> None:
    collector = MetricsCollector()
    collector.increment("api_calls", delta=5)
    collector.increment("api_calls", delta=3)

    summary = collector.summary()
    assert summary["counters"]["api_calls:"] == 8


# 功能：验证 timed 上下文管理器自动记录执行时间
# 设计：在 timed 块中 sleep 已知时间，验证记录的延迟接近实际值
def test_timed_context_manager() -> None:
    collector = get_collector()
    collector.reset()

    with timed("sleep_test"):
        time.sleep(0.1)

    stats = collector.get_latency_stats("sleep_test")
    assert stats is not None
    assert stats.count == 1
    assert 95 <= stats.p50 <= 150  # 允许一定误差（100ms ± 50ms）


# 功能：验证 async_timed 正确记录异步函数执行时间
# 设计：在异步上下文中 sleep，验证计时器工作
async def test_async_timed_context_manager() -> None:
    collector = get_collector()
    collector.reset()

    async with async_timed("async_sleep"):
        await asyncio.sleep(0.1)

    stats = collector.get_latency_stats("async_sleep")
    assert stats is not None
    assert stats.count == 1
    assert 95 <= stats.p50 <= 150


# 功能：验证 MetricsCollector.summary() 生成完整的性能报告
# 设计：记录多种指标后调用 summary，验证输出包含所有数据
def test_metrics_collector_summary() -> None:
    collector = MetricsCollector()
    collector.record_timing("op1", 123.45)
    collector.record_timing("op1", 234.56)
    collector.increment("counter1", 10)

    summary = collector.summary()
    assert "timings" in summary
    assert "counters" in summary
    assert "op1" in summary["timings"]
    assert summary["timings"]["op1"]["count"] == 2
    assert "counter1:" in summary["counters"]


# 功能：验证 MetricsCollector.reset() 清空所有指标
# 设计：记录数据后调用 reset，验证后续查询返回空
def test_metrics_collector_reset() -> None:
    collector = MetricsCollector()
    collector.record_timing("test", 100)
    collector.increment("count", 1)

    collector.reset()

    assert collector.get_latency_stats("test") is None
    assert collector.summary() == {"timings": {}, "counters": {}}


# 功能：验证带标签的指标记录
# 设计：使用不同标签记录同名指标，验证标签被正确追踪
def test_metrics_with_tags() -> None:
    collector = MetricsCollector()
    collector.record_timing("llm_call", 100, tags={"model": "sonnet"})
    collector.record_timing("llm_call", 200, tags={"model": "opus"})

    # 验证原始指标包含标签信息
    metrics = collector.export_metrics()
    assert len(metrics) == 2
    assert any(m.tags.get("model") == "sonnet" for m in metrics)
    assert any(m.tags.get("model") == "opus" for m in metrics)


# 功能：验证空数据集的边界情况
# 设计：查询不存在的指标，验证返回 None 而非崩溃
def test_empty_metrics_returns_none() -> None:
    collector = MetricsCollector()
    assert collector.get_latency_stats("nonexistent") is None
