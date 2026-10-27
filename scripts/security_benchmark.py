from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

from claudedock.core.llm.types import ToolCallBlock
from claudedock.core.permissions.manager import PermissionManager
from claudedock.core.security.context import SecurityContext
from claudedock.core.security.gate import SecurityGate
from claudedock.core.tools.builtin.read_file import ReadFileTool


# 离线测量统一网关纯读取决策开销，不执行工具或调用任何外部模型
def main() -> None:
    root = Path.cwd().resolve()
    manager = PermissionManager(project_root=root)
    context = SecurityContext(
        manager.principal_id, root, "benchmark", "offline", "offline:call",
        policy_revision=manager.policy_revision, deadline=time.monotonic() + 60,
    )
    gate, tool = SecurityGate(), ReadFileTool(root)
    call = ToolCallBlock("call", "read_file", {"path": "README.md"})
    samples = []
    for _ in range(1000):
        start = time.perf_counter()
        decision = gate.evaluate(context, call, tool, manager)
        samples.append((time.perf_counter() - start) * 1000)
        assert decision.action == "allow"
    samples.sort()
    print(json.dumps({
        "kind": "offline_security_gate_benchmark", "samples": len(samples),
        "median_ms": round(statistics.median(samples), 4),
        "p95_ms": round(samples[int(len(samples) * 0.95)], 4),
        "model_requests": 0, "external_effects": 0,
        "limitations": "Static read gate only; no AI latency, MCP, business backend or sandbox execution measured.",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
