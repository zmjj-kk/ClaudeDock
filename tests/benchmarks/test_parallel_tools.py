from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from tests.security_helpers import approving_manager
from tests.unit.test_tool_scheduler import ProbeTool

from claudedock.core.config import ToolExecutionConfig
from claudedock.core.events.bus import EventBus
from claudedock.core.llm.types import ToolCallBlock
from claudedock.core.monitoring.metrics import MetricsCollector
from claudedock.core.permissions.manager import PermissionManager
from claudedock.core.tools.base import ToolResult
from claudedock.core.tools.builtin.read_file import ReadFileTool
from claudedock.core.tools.invocation import invoke_tool
from claudedock.core.tools.registry import ToolRegistry
from claudedock.core.tools.scheduler import ToolExecutionScheduler


@pytest.mark.parametrize("sample", ["simulated_async_20ms", "local_files_256KiB"])
# 功能：生成可重复的模拟异步和真实本地读取串行并行 P50/P95 数据
# 设计：两模式走相同安全网关，每模式七次采样，不用不稳定的加速比阈值作为正确性断言
async def test_serial_parallel_benchmark(tmp_path: Path, sample: str) -> None:
    registry = ToolRegistry()
    bus = EventBus()
    if sample == "local_files_256KiB":
        for index in range(4):
            (tmp_path / str(index)).write_text("x" * (256 * 1024))
        registry.register(ReadFileTool(tmp_path))
        name = "read_file"
        manager = PermissionManager(project_root=tmp_path, rules=())
    else:
        tool = ProbeTool(tmp_path)

        # 固定延迟只作为调度能力样本，不代表真实业务性能
        async def delayed(params):
            await asyncio.sleep(0.02)
            return ToolResult("ok")

        tool.invoke = delayed
        registry.register(tool, trusted_concurrency=True)
        name = "probe"
        manager = approving_manager(bus)
        manager.project_root = tmp_path

    # 两种模式复用完整生产调用链，差别仅为调度开关
    async def invoke(tc, slots):
        return await invoke_tool(
            registry, tc, bus, "benchmark", permission_manager=manager, execution_slots=slots
        )

    report = {"sample": sample, "calls_per_batch": 4, "samples_per_mode": 7, "modes": {}}
    for enabled in (False, True):
        timings = MetricsCollector()
        scheduler = ToolExecutionScheduler(
            registry,
            invoke,
            bus,
            asyncio.Semaphore(4),
            ToolExecutionConfig(parallel_enabled=enabled),
        )
        failed = 0
        total_s = 0.0
        for repetition in range(7):
            calls = [
                ToolCallBlock(f"{enabled}-{repetition}-{i}", name, {"path": str(i)})
                for i in range(4)
            ]
            started = time.perf_counter()
            results = await scheduler.execute(calls, run_id="benchmark")
            elapsed = time.perf_counter() - started
            total_s += elapsed
            timings.record_timing("batch", elapsed * 1000)
            failed += sum(result.is_error for result in results)
        stats = timings.get_latency_stats("batch")
        assert stats is not None and failed == 0
        report["modes"]["parallel" if enabled else "serial"] = {
            "p50_ms": round(stats.p50, 3),
            "p95_ms": round(stats.p95, 3),
            "calls_per_second": round(28 / total_s, 3),
            "error_count": failed,
        }
    print("BENCHMARK_JSON=" + json.dumps(report, sort_keys=True))
