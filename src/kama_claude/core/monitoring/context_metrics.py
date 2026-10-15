from __future__ import annotations

from typing import Any

from kama_claude.core.monitoring.metrics import get_collector


# 将上下文事件转换为低基数统计，标签中不包含正文、路径或会话标识
async def observe_context(event: Any) -> None:
    collector = get_collector()
    kind = event.type
    if kind == "context.budget_checked":
        collector.increment("context_budget_checks")
        if event.over_limit:
            collector.increment("context_preflight_over_limit")
    elif kind == "context.pruned":
        collector.increment("context_pruned_results", event.count)
        collector.increment("context_pruned_estimated_tokens", event.saved_tokens)
    elif kind == "context.compacted":
        collector.increment("context_compact_attempts", tags={"outcome": "success"})
        collector.record_timing("context_compaction", event.elapsed_ms, {"outcome": "success"})
        collector.increment(
            "context_compact_saved_estimated_tokens",
            max(0, event.original_tokens - (event.projection_tokens or event.summary_tokens)),
        )
    elif kind == "context.compaction_failed":
        collector.increment("context_compact_attempts", tags={"outcome": "failed"})
        collector.record_timing("context_compaction", event.elapsed_ms, {"outcome": "failed"})
        if event.circuit_open:
            collector.increment("context_compact_circuit_open")
    elif kind == "context.memory_updated":
        collector.increment("context_memory_updates")
    elif kind == "context.cache_usage":
        collector.increment("context_cache_read_tokens", event.cache_read)
        collector.increment("context_cache_creation_tokens", event.cache_create)
