from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import resource
import tempfile
import time
from pathlib import Path

from kama_claude.core.config import KamaConfig
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, UsageStats
from kama_claude.core.runner import AgentRunner
from kama_claude.core.transport.socket_client import SocketClient
from kama_claude.core.transport.socket_server import SocketServer


class OfflineProvider:
    # 使用模型替身测量运行编排与持久化开销，不调用付费 API
    async def chat(self, **kwargs: object) -> LlmResponse:
        return LlmResponse(stop_reason="end_turn", text="done", usage=UsageStats(10, 5))


# 返回样本的实际分位数，不用聚合均值代替延迟分布
def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


# 汇总原始耗时样本的 P50/P95/P99
def latency(values: list[float]) -> dict[str, float]:
    return {
        "p50_ms": percentile(values, 0.50),
        "p95_ms": percentile(values, 0.95),
        "p99_ms": percentile(values, 0.99),
        "max_ms": max(values),
    }


# 对真正 TCP IPC 并发请求与重复 Agent Run 执行本机稳定性基线
async def benchmark(runs: int, clients: int) -> dict[str, object]:
    token = "benchmark-" + "0" * 32
    server = SocketServer("127.0.0.1", 0, auth_token=token)

    # 返回标准 JSON-RPC 结果以隔离 IPC 协议开销
    async def ping(params: dict[str, object]) -> dict[str, bool]:
        return {"ok": True}

    server.register("core.ping", ping)
    await server.start()
    assert server._server is not None
    port = server._server.sockets[0].getsockname()[1]
    samples: list[float] = []

    # 每个客户端串行发送 32 帧，避免测量触发保护性速率限制
    async def client_work() -> None:
        client = SocketClient("127.0.0.1", port, token=token, request_timeout=10)
        await client.connect()
        reader = asyncio.create_task(client.run_event_loop())
        try:
            for _ in range(32):
                started = time.perf_counter()
                result = await client.send_command("core.ping", {})
                assert result["ok"]
                samples.append((time.perf_counter() - started) * 1000)
        finally:
            await client.close()
            await reader

    start = time.perf_counter()
    try:
        await asyncio.gather(*(client_work() for _ in range(clients)))
    finally:
        await server.stop()
    ipc_seconds = time.perf_counter() - start
    assert not server._connections and not server._requests
    bus = EventBus()
    config = KamaConfig()
    config.skill.enabled = False
    fd_before = len(os.listdir("/proc/self/fd"))
    run_samples: list[float] = []
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="kama-stability-") as directory:
        runner = AgentRunner(config, bus=bus, provider=OfflineProvider(), runs_dir=Path(directory))
        for index in range(runs):
            before = time.perf_counter()
            outcome = await runner.run_and_capture("offline stability", run_id=f"bench-{index}")
            assert outcome.status == "success"
            assert bus.subscriber_count == 0
            run_samples.append((time.perf_counter() - before) * 1000)
        await runner.aclose()
    run_seconds = time.perf_counter() - start
    gc.collect()
    fd_after = len(os.listdir("/proc/self/fd"))
    assert fd_after <= fd_before
    return {
        "scope": "local offline orchestration; does not include LLM/API or production network latency",
        "run_count": runs,
        "client_count": clients,
        "ipc_requests": len(samples),
        "ipc_latency": latency(samples),
        "ipc_requests_per_second": len(samples) / ipc_seconds,
        "run_latency": latency(run_samples),
        "run_total_seconds": run_seconds,
        "event_subscribers_after_runs": bus.subscriber_count,
        "open_fds_before": fd_before,
        "open_fds_after": fd_after,
        "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }


# 将可复现基线写入指定 JSON 文件
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1000)
    parser.add_argument("--clients", type=int, default=32)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.clients <= 64 or args.runs < 1:
        parser.error("clients must be 1..64 and runs must be positive")
    result = asyncio.run(benchmark(args.runs, args.clients))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
