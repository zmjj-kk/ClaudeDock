from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any


class ContextTooLarge(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelCapacity:
    window: int | None
    max_output: int = 32768


@dataclass(frozen=True)
class ContextBudget:
    window: int | None
    reserved_output: int
    safety_margin: int
    system_tokens: int
    tools_tokens: int
    history_tokens: int
    estimate_method: str = "utf8_upper_bound"
    schema_version: int = 1

    # 汇总单请求输入占用，不包含累计执行令牌
    @property
    def input_tokens(self) -> int:
        return self.system_tokens + self.tools_tokens + self.history_tokens

    # 计算减去实际输出预留与安全裕度后的输入容量
    @property
    def max_input(self) -> int | None:
        if self.window is None:
            return None
        return self.window - self.reserved_output - self.safety_margin

    # 未知模型保持不可判定状态，禁止作为零占用放行
    @property
    def over_limit(self) -> bool:
        return self.max_input is None or self.input_tokens > self.max_input

    # 使用完整窗口作为水位分母，输入容量另行硬校验
    @property
    def ratio(self) -> float | None:
        return self.input_tokens / self.window if self.window else None


# 使用 UTF-8 字节上界回退，避免中文字符数除四的系统性低估
def estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return len(text.encode("utf-8")) + 16


# 统计系统、工具与完整消息构成并预留实际请求输出
def check_budget(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    system: str,
    capacity: ModelCapacity,
    safety_ratio: float = 0.05,
) -> ContextBudget:
    return ContextBudget(
        window=capacity.window,
        reserved_output=capacity.max_output,
        safety_margin=math.ceil((capacity.window or 0) * safety_ratio),
        system_tokens=estimate_tokens(system),
        tools_tokens=estimate_tokens(tools),
        history_tokens=estimate_tokens(messages),
    )


# 从包装提供者中取得声明的模型容量，测试旧 stub 可保持未知状态
def provider_capacity(provider: Any, window: int | None = None) -> ModelCapacity:
    seen: set[int] = set()
    current = provider
    while id(current) not in seen and len(seen) < 8:
        seen.add(id(current))
        capacity = getattr(current, "context_capacity", None)
        if isinstance(capacity, ModelCapacity):
            return ModelCapacity(window or capacity.window, capacity.max_output)
        namespace = getattr(current, "__dict__", None)
        inner = namespace.get("_inner") if isinstance(namespace, dict) else None
        if inner is None:
            break
        current = inner
    return ModelCapacity(window)
