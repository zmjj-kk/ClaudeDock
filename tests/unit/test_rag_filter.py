from __future__ import annotations

from kama_claude.core.rag.filter import canonical, is_range, matches


# 功能：验证 canonical 把不同类型统一为可比较字符串
# 设计：bool 转小写字符串、数值十进制、字符串原样，覆盖三种植入路径
def test_canonical() -> None:
    assert canonical(True) == "true"
    assert canonical(False) == "false"
    assert canonical(10) == "10"
    assert canonical(2.5) == "2.5"
    assert canonical("研发") == "研发"


# 功能：验证 is_range 只识别含范围操作符的字典
# 设计：含 gte 判真，普通值/空字典判假
def test_is_range() -> None:
    assert is_range({"gte": 1}) is True
    assert is_range({"lte": 1}) is True
    assert is_range("x") is False
    assert is_range({}) is False


# 功能：验证 matches 等值比较时类型经 canonical 归一（数值 10 命中字符串 "10"）
# 设计：跨类型等值、列表任一、范围三路断言
def test_matches_equality_and_list() -> None:
    assert matches(10, "10") is True
    assert matches(True, "true") is True
    assert matches("研发", ["研发", "财务"]) is True
    assert matches("运维", ["研发", "财务"]) is False


# 功能：验证 matches 范围比较：数值优先、字符串退字典序、边界开闭
# 设计：gte/lte/gt/lt 四操作符逐一断言含边界与越界
def test_matches_range() -> None:
    assert matches(2025, {"gte": 2025}) is True
    assert matches(2024, {"gte": 2025}) is False
    assert matches(2025, {"lte": 2025}) is True
    assert matches(2025, {"gt": 2025}) is False
    assert matches(2026, {"gt": 2025}) is True
    assert matches(2025, {"lt": 2025}) is False
    # 字符串走字典序
    assert matches("2026-01", {"gte": "2025-01"}) is True
