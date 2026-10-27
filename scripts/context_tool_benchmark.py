from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from claudedock.core.compact.token_budget import estimate_tokens
from claudedock.core.tools.base import BaseTool, ToolResult
from claudedock.core.tools.registry import ToolRegistry


class BenchmarkTool(BaseTool):
    description = "Synthetic authorized capability " + "details " * 100
    input_schema = {"type": "object", "properties": {"query": {"type": "string"}}}

    # 生成稳定的合成工具名称，不连接任何外部服务
    def __init__(self, name: str) -> None:
        self.name = name

    # 离线评估只查询 Schema，不执行真实操作
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult("synthetic")


# 分档评估工具模式体积与精确名称发现，绝不作为模型质量或费用评测
def main() -> None:
    parser = argparse.ArgumentParser(description="Offline 10/50/200 tool discovery benchmark")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rows = []
    for count in (10, 50, 200):
        registry = ToolRegistry()
        for index in range(count):
            registry.register(BenchmarkTool(f"special_{index}"))
        full = estimate_tokens(registry.tool_schemas())
        registry.enable_deferred({"special_0"})
        requested = f"special_{count - 1}"
        started = time.perf_counter()
        result = registry.discover(requested)
        found = bool(result) and result[0]["name"] == requested
        registry.resolve_tools([requested])
        rows.append({
            "tools": count, "full_estimate": full,
            "selected_estimate": estimate_tokens(registry.tool_schemas()),
            "exact_name_top1": found,
            "search_resolve_ms": round((time.perf_counter() - started) * 1000, 3),
        })
    text = json.dumps({
        "schema_version": 1, "estimate_method": "utf8_upper_bound", "api_calls": 0,
        "limitations": "Synthetic exact-name retrieval; no model quality or real cost claims",
        "cases": rows,
    }, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
