from __future__ import annotations

from kama_claude.core.rag.filter import (
    canonical,
    canonical_values,
    is_numeric_range,
    is_range,
    matches,
    select,
)


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


# 功能：验证元数据取值本身为列表时按包含语义匹配（tags 这类标签字段）
# 设计：标量条件、列表条件、范围条件三种 filter 对同一列表取值断言，
#       覆盖"列表在左、条件在右"这一此前完全失效的路径
def test_matches_list_metadata_value() -> None:
    assert matches(["ml", "nlp"], "ml") is True
    assert matches(["ml", "nlp"], ["nlp", "x"]) is True
    assert matches(["ml", "nlp"], "ops") is False
    assert matches(["ml", 2026], {"gte": 2025}) is True


# 功能：验证类型不可比时判为不匹配（fail-closed），不因退化为字典序而静默放行
# 设计：非数值字符串对数值边界、以及嵌套/None 等畸形条件，均断言 False 而非异常或误命中
def test_matches_incomparable_is_false() -> None:
    assert matches("abc", {"gte": 5}) is False
    assert matches(2025, {"gte": {"gte": 2000}}) is False
    assert matches(None, {"gte": 1}) is False
    assert matches("abc", {"lte": "abd"}) is True  # 同为文本仍按字典序


# 功能：验证 is_numeric_range 判定范围边界能否交给 Qdrant 的数值 Range 表达
# 设计：整型/数字串可通过，YYYY-MM 时间串与布尔被拒，空边界字典判否
def test_is_numeric_range() -> None:
    assert is_numeric_range({"gte": 2025}) is True
    assert is_numeric_range({"gte": "2026"}) is True
    assert is_numeric_range({"gte": "2026-03"}) is False
    assert is_numeric_range({"gte": True}) is False
    assert is_numeric_range({}) is False


# 功能：验证 select 求各字段命中下标的交集，列表元数据同样参与匹配
# 设计：三份元数据按单字段、多字段交集与列表字段三种查询断言下标集合
def test_select_intersects_fields() -> None:
    metas = [
        {"dept": "研发", "tags": ["ml", "nlp"]},
        {"dept": "财务", "tags": ["ml"]},
        {"dept": "研发", "tags": ["ops"]},
    ]
    assert select(metas, {"dept": "研发"}) == {0, 2}
    assert select(metas, {"dept": "研发", "tags": "ml"}) == {0}
    assert select(metas, {"tags": ["nlp", "ops"]}) == {0, 2}
    assert select(metas, {}) == {0, 1, 2}


# 功能：验证 canonical_values 把列表取值展开为逐元素字符串，标量包装为单元素
# 设计：列表展开用于倒排索引逐元素入桶与词汇表枚举，避免整个列表被串成一个键
def test_canonical_values() -> None:
    assert canonical_values(["a", "b"]) == ["a", "b"]
    assert canonical_values(10) == ["10"]
    assert canonical_values(True) == ["true"]
