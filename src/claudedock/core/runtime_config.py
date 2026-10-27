from __future__ import annotations

import math
import os
from dataclasses import dataclass, fields
from typing import Any


@dataclass
class RuntimeConfig:
    max_cost_usd: float = 0.0
    max_model_calls: int = 128
    max_llm_requests: int = 8
    max_tool_requests: int = 16
    max_runs: int = 8
    max_jobs: int = 32
    max_connections: int = 64
    max_requests: int = 32
    request_timeout_s: float = 3600.0
    run_timeout_s: float = 1800.0
    max_tool_calls: int = 256
    max_tokens: int = 1_000_000
    stagnation_limit: int = 3


# 从 TOML 或环境变量读取正数限额，拒绝布尔值、NaN 和无界配置
def apply_runtime(
    config: RuntimeConfig, values: dict[str, Any], *, environment: bool = False
) -> None:
    names = {f.name for f in fields(config)}
    if set(values) - names:
        raise SystemExit("Unknown runtime configuration key")
    for name in names:
        value = os.getenv(f"CLAUDEDOCK_RUNTIME_{name.upper()}") if environment else values.get(name)
        if value is None:
            continue
        try:
            if isinstance(value, bool):
                raise ValueError("boolean limit")
            number = float(value)
            if (number < 0 if name == "max_cost_usd" else number <= 0) or not math.isfinite(number):
                raise ValueError("non-positive limit")
            if name.endswith("_s") or name == "max_cost_usd":
                setattr(config, name, number)
            else:
                if not number.is_integer():
                    raise ValueError("non-integral limit")
                setattr(config, name, int(number))
        except (TypeError, ValueError) as exc:
            raise SystemExit(f"Invalid runtime.{name}: expected a positive limit") from exc
