from __future__ import annotations

from pathlib import Path

from kama_claude.core.config import MemoryConfig
from kama_claude.core.memory.sqlite_store import MemorySqliteStore
from kama_claude.core.memory.store import MemoryStore
from kama_claude.core.memory.vector_store import MemoryVectorStore
from kama_claude.core.rag.index import Bm25Index
from kama_claude.core.tools.builtin.memory_query import MemoryQueryTool
from kama_claude.core.tools.builtin.memory_save import MemorySaveTool
from kama_claude.core.tools.builtin.memory_search import MemorySearchTool


# 组装带双层存储的 MemoryStore 供工具测试复用
def _make_store(tmp_path: Path) -> MemoryStore:
    config = MemoryConfig(store_dir=str(tmp_path))
    sqlite = MemorySqliteStore(tmp_path / "memories.db")
    vector = MemoryVectorStore(Bm25Index(), chunk_size=200, chunk_overlap=20)
    return MemoryStore(config, sqlite_store=sqlite, vector_store=vector)


# 功能：验证 memory_save 写入记忆并返回保存确认
# 设计：调用 invoke 写入 preference，断言非错误且 content 含 saved 标记
async def test_memory_save(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    await store.load()
    result = await MemorySaveTool(store).invoke(
        {"content": "用户偏好英文", "kind": "preference"}
    )
    assert not result.is_error
    assert "saved" in result.content


# 功能：验证 memory_save 空内容返回错误
# 设计：content 为空白，断言 is_error 且 error_type=runtime_error
async def test_memory_save_empty_content(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    await store.load()
    result = await MemorySaveTool(store).invoke({"content": "   "})
    assert result.is_error
    assert result.error_type == "runtime_error"


# 功能：验证 memory_save 重复写入返回已存在提示而非报错
# 设计：同 content 两次 invoke，第二次 content 含 duplicate 且非错误
async def test_memory_save_duplicate(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    await store.load()
    tool = MemorySaveTool(store)
    await tool.invoke({"content": "用户偏好英文", "kind": "preference"})
    result = await tool.invoke({"content": "用户偏好英文", "kind": "preference"})
    assert not result.is_error
    assert "duplicate" in result.content


# 功能：验证 memory_save 在模型未给 project 时落回构造器注入的默认项目
# 设计：MemorySaveTool(store, default_project="p") 不带 project 调用，写入条目 project 应为 p
async def test_memory_save_uses_default_project(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    await store.load()
    result = await MemorySaveTool(store, default_project="p").invoke(
        {"content": "用户偏好英文", "kind": "preference"}
    )
    assert not result.is_error
    entries = await store.query(kind="preference", project="p")
    assert [e.content for e in entries] == ["用户偏好英文"]


# 功能：验证 memory_query 按 kind 返回结构化条目
# 设计：写入 preference 与 fact 各一条，query kind=preference 只含 preference 正文
async def test_memory_query_by_kind(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    await store.load()
    await store.add("用户偏好英文", kind="preference")
    await store.add("项目使用 Python", kind="fact")
    result = await MemoryQueryTool(store).invoke({"kind": "preference"})
    assert "用户偏好英文" in result.content
    assert "项目使用 Python" not in result.content


# 功能：验证 memory_query 无命中返回提示而非错误
# 设计：query 一个不存在的 kind，断言非错误且含 No matching
async def test_memory_query_no_match(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    await store.load()
    result = await MemoryQueryTool(store).invoke({"kind": "knowledge"})
    assert not result.is_error
    assert "No matching" in result.content


# 功能：验证 memory_search 语义召回相关记忆
# 设计：写入 knowledge 条目后 search "文档" 能召回含该词的条目正文
async def test_memory_search(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    await store.load()
    await store.add("产品文档位于 docs 目录", kind="knowledge")
    result = await MemorySearchTool(store, default_top_k=5).invoke({"query": "文档"})
    assert not result.is_error
    assert "产品文档" in result.content
