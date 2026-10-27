from __future__ import annotations

from typing import Any

# 元数据取值类型：字符串、整数、浮点、布尔，或字符串列表（列表=任一匹配）
MetaValue = str | int | float | bool | list[str]


# 把元数据/过滤值规范化为可比较的字符串：bool 小写、数值十进制、字符串原样
def canonical(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# 把元数据取值展开为 canonical 字符串列表：列表值逐元素展开，标量视为单元素
def canonical_values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [canonical(v) for v in value if not isinstance(v, (list, dict))]
    return [canonical(value)]


# 判断过滤值是否为范围条件（字典且含 gte/lte/gt/lt 之一）
def is_range(value: Any) -> bool:
    return isinstance(value, dict) and any(k in value for k in ("gte", "lte", "gt", "lt"))


# 判断单个元数据取值是否命中过滤条件：列表值按包含语义，其余走等值 / 列表任一 / 范围
def matches(value: Any, cond: Any) -> bool:
    # 元数据自身是列表（如 tags）时，任一元素命中即算命中
    if isinstance(value, list):
        return any(matches(v, cond) for v in value)
    if is_range(cond):
        return _match_range(value, cond)
    if isinstance(cond, list):
        return canonical(value) in {canonical(v) for v in cond}
    return canonical(value) == canonical(cond)


# 返回 metadatas 中命中全部过滤条件的下标集合；任一字段不满足即整体剔除
def select(metadatas: list[dict[str, Any]], cond: dict[str, Any]) -> set[int]:
    out = set(range(len(metadatas)))
    for key, val in cond.items():
        out &= {i for i, m in enumerate(metadatas) if key in m and matches(m[key], val)}
        if not out:
            return set()
    return out


# 判断范围边界是否全部可按数值解释；Qdrant 的 Range 只接受数值，非数值须回落客户端过滤
def is_numeric_range(cond: dict[str, Any]) -> bool:
    bounds = [cond[k] for k in ("gte", "lte", "gt", "lt") if k in cond]
    return bool(bounds) and all(_as_number(b) is not None for b in bounds)


# 范围匹配：逐边界比较，缺省边界视为不限制；边界不可比较时判不匹配
def _match_range(value: Any, cond: dict[str, Any]) -> bool:
    if "gte" in cond and not _satisfies(value, cond["gte"], "gte"):
        return False
    if "lte" in cond and not _satisfies(value, cond["lte"], "lte"):
        return False
    if "gt" in cond and not _satisfies(value, cond["gt"], "gt"):
        return False
    if "lt" in cond and not _satisfies(value, cond["lt"], "lt"):
        return False
    return True


# 单边界判定：value 相对 bound 满足 op 时返回 True，不可比较一律返回 False（不得静默放行）
def _satisfies(value: Any, bound: Any, op: str) -> bool:
    order = _order(value, bound)
    if order is None:
        return False
    if op == "gte":
        return order >= 0
    if op == "lte":
        return order <= 0
    if op == "gt":
        return order > 0
    return order < 0


# 计算 value 相对 bound 的序关系（-1/0/1）：两侧同为数值按数值比，同为文本按字典序；
# 一侧可转数值而另一侧不可（类型不匹配）返回 None，由调用方判为不匹配
def _order(value: Any, bound: Any) -> int | None:
    a, b = _as_number(value), _as_number(bound)
    if a is None and b is None:
        a_str, b_str = _as_text(value), _as_text(bound)
        if a_str is None or b_str is None:
            return None
        return (a_str > b_str) - (a_str < b_str)
    if a is None or b is None:
        return None
    return (a > b) - (a < b)


# 尝试把值解析为数值；bool 不参与数值比较（交给文本分支保持 true/false 语义），失败返回 None
def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# 把标量转为可比较文本；None、列表、字典等非标量返回 None 表示不可比较
def _as_text(value: Any) -> str | None:
    if isinstance(value, (str, int, float, bool)):
        return canonical(value)
    return None
