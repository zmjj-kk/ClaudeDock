from __future__ import annotations

import math
from typing import Any


# 仅接受显式配置的每百万 Token 价格，拒绝猜测未知模型的费用
def validate_prices(values: Any) -> dict[str, dict[str, float]]:
    if not isinstance(values, dict):
        raise SystemExit("llm.prices must be a model-to-price table")
    prices: dict[str, dict[str, float]] = {}
    fields = {"input", "output", "cache_read", "cache_create"}
    for model, entry in values.items():
        if not isinstance(model, str) or not isinstance(entry, dict) or set(entry) != fields:
            raise SystemExit("Each model price requires input/output/cache_read/cache_create")
        converted = {}
        for key, value in entry.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SystemExit("Token price must be numeric")
            if value < 0 or not math.isfinite(value):
                raise SystemExit("Token price must be finite and non-negative")
            converted[key] = float(value)
        prices[model] = converted
    return prices
