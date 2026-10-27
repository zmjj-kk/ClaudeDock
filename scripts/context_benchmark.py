from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any


# 直接加载无第三方依赖的纯算法，避免离线基准意外初始化模型或数据库
def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load benchmark module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# 将固定场景转换成完整工具消息组，保留近期原文和签名测试数据
def _history(task: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for turn in range(task["turns"]):
        messages.extend([
            {"role": "user", "content": (
                task["original_goal"] if turn == 0 else f"Follow-up {turn}"
            )},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "opaque", "signature": "fixed-signature"},
                {"type": "tool_use", "id": f"call-{turn}", "name": "stub", "input": {}},
            ]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": f"call-{turn}",
                "content": task["untrusted_input"] + " observation" * 1000
                + f"\n[tool-result-ref:fixed-{turn}; synthetic benchmark reference]",
            }]},
        ])
    return messages


# 运行固定离线回放，只报告回退估算与前处理开销，不作模型质量结论
def main() -> None:
    parser = argparse.ArgumentParser(description="Offline context algorithms; no API calls")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    compact = root / "src/claudedock/core/compact"
    budget = _load("context_benchmark_budget", compact / "token_budget.py")
    micro = _load("context_benchmark_micro", compact / "microcompact.py")
    fixture = root / "tests/fixtures/context_eval/tasks.json"
    raw = fixture.read_bytes()
    tasks = json.loads(raw)
    rows = []
    for task in tasks:
        messages = _history(task)
        started = time.perf_counter()
        candidate, count = micro.microcompact(messages, 4)
        elapsed_ms = (time.perf_counter() - started) * 1000
        assert candidate[-12:] == messages[-12:]
        micro.validate_messages(candidate)
        rows.append({
            "id": task["id"], "turns": task["turns"], "pruned_results": count,
            "pre_estimate": budget.estimate_tokens(messages),
            "post_estimate": budget.estimate_tokens(candidate),
            "elapsed_ms": round(elapsed_ms, 3),
        })
    text = json.dumps({
        "schema_version": 1, "estimate_method": "utf8_upper_bound",
        "fixture_sha256": hashlib.sha256(raw).hexdigest(),
        "api_calls": 0, "cases": rows,
        "limitations": "Synthetic references; not a storage, LLM quality or cost benchmark",
    }, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
