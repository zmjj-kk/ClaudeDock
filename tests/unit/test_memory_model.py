from __future__ import annotations

from claudedock.core.memory.model import MemoryEntry, new_memory_id


# 功能：验证 new_memory_id 由内容哈希派生，稳定且带 mem_ 前缀与固定长度
# 设计：同一内容两次调用结果一致、不同内容结果不同，断言前缀与 12 位 hex 后缀
def test_new_memory_id_stable() -> None:
    a = new_memory_id("user prefers English")
    b = new_memory_id("user prefers English")
    c = new_memory_id("user prefers Chinese")
    assert a == b
    assert a != c
    assert a.startswith("mem_")
    assert len(a) == 4 + 12


# 功能：验证 MemoryEntry 默认字段：status=active、importance=0.5、时间戳非空
# 设计：只给 id/content/kind 构造，断言其余字段落入数据模型约定的默认值
def test_memory_entry_defaults() -> None:
    e = MemoryEntry(id="mem_abc", content="x", kind="preference")
    assert e.status == "active"
    assert e.importance == 0.5
    assert e.project == ""
    assert e.tags == []
    assert e.created_at
    assert e.updated_at
    assert e.last_accessed_at


# 功能：验证 to_dict 返回 tags 的副本，改动副本不影响原条目
# 设计：to_dict 后向返回的 tags 列表追加元素，断言原条目 tags 不变，避免共享引用污染
def test_to_dict_tags_are_a_copy() -> None:
    e = MemoryEntry(id="m1", content="content", kind="fact", tags=["a", "b"])
    data = e.to_dict()
    data["tags"].append("x")
    assert e.tags == ["a", "b"]


# 功能：验证 from_dict 能还原 to_dict 输出的完整字段
# 设计：roundtrip 后断言关键字段一致，确认序列化/反序列化无字段丢失
def test_memory_entry_roundtrip() -> None:
    e = MemoryEntry(
        id="m1", content="内容", kind="decision", project="p",
        importance=0.8, tags=["a"], status="active",
    )
    restored = MemoryEntry.from_dict(e.to_dict())
    assert restored.id == "m1"
    assert restored.content == "内容"
    assert restored.kind == "decision"
    assert restored.project == "p"
    assert restored.importance == 0.8
    assert restored.tags == ["a"]


# 功能：验证 from_dict 对缺字段与 tags 类型漂移的容忍
# 设计：传入最小 dict 与 tags 为字符串的 dict，断言不抛异常且回退到安全默认值
def test_memory_entry_from_dict_tolerates_bad_input() -> None:
    e = MemoryEntry.from_dict({"id": "m2"})
    assert e.content == ""
    assert e.tags == []
    assert e.importance == 0.5
    e2 = MemoryEntry.from_dict({"id": "m3", "tags": "not-a-list"})
    assert e2.tags == []
