from __future__ import annotations

from typing import Any

# 元数据取值类型：字符串、整数、浮点、布尔，或字符串列表（列表=任一匹配）
MetaValue = str | int | float | bool | list[str]


# 把元数据/过滤值规范化为可比较的字符串：bool 小写、数值十进制、字符串原样
def canonical(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# 判断过滤值是否为范围条件（字典且含 gte/lte/gt/lt 之一）
def is_range(value: Any) -> bool:
    return isinstance(value, dict) and any(k in value for k in ("gte", "lte", "gt", "lt"))


# 判断单个元数据取值是否命中过滤条件：等值 / 列表任一 / 范围
def matches(value: Any, cond: Any) -> bool:
    if is_range(cond):
        return _match_range(value, cond)
    if isinstance(cond, list):
        return canonical(value) in {canonical(v) for v in cond}
    return canonical(value) == canonical(cond)


# 范围匹配：逐边界比较，缺省边界视为不限制
def _match_range(value: Any, cond: dict[str, Any]) -> bool:
    if "gte" in cond and _order(value, cond["gte"]) < 0:
        return False
    if "lte" in cond and _order(value, cond["lte"]) > 0:
        return False
    if "gt" in cond and _order(value, cond["gt"]) <= 0:
        return False
    if "lt" in cond and _order(value, cond["lt"]) >= 0:
        return False
    return True


# 计算 value 相对 bound 的序关系（-1/0/1）：优先按数值，退化字符串字典序
def _order(value: Any, bound: Any) -> int:
    try:
        a = float(value)
        b = float(bound)
    except (TypeError, ValueError):
        a_str, b_str = canonical(value), canonical(bound)
        return (a_str > b_str) - (a_str < b_str)
    return (a > b) - (a < b)
