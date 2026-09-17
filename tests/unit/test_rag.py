from __future__ import annotations

from pathlib import Path

from kama_claude.core.rag.store import RagStore, _split_chunks, _tokenize
from kama_claude.core.rag.tool import RagSearchTool


# 功能：验证分词器同时产出英文单词与中文字符 bigram
# 设计：用中英混合文本断言英文按单词切分、中文按相邻两字切分，覆盖两条语言路径
def test_tokenize_mixed_language() -> None:
    tokens = _tokenize("机器学习 Machine Learning")
    assert "machine" in tokens
    assert "learning" in tokens
    assert "机器" in tokens
    assert "器学" in tokens


# 功能：验证文本恰好整除时按 size 切分为等长块
# 设计：8 字符文本 size=4/overlap=0，断言切出两个等长块
def test_split_chunks_exact() -> None:
    assert _split_chunks("abcdefgh", 4, 0) == ["abcd", "efgh"]


# 功能：验证相邻块按 overlap 重叠，避免语义在边界被切断
# 设计：断言第 1 块与第 2 块共享 overlap 个字符（"cd"），而非只检查完整列表
def test_split_chunks_overlap() -> None:
    chunks = _split_chunks("abcdefghij", 4, 2)
    assert chunks[0] == "abcd"
    assert chunks[1] == "cdef"
    assert chunks[0][-2:] == chunks[1][:2] == "cd"


# 功能：验证短于 size 的文本整体作为单块返回，空文本返回空列表
# 设计：用纯空白文本断言不产生空 chunk，避免污染索引
def test_split_chunks_short_and_empty() -> None:
    assert _split_chunks("abc", 4, 2) == ["abc"]
    assert _split_chunks("   ", 4, 2) == []


# 功能：验证 load_dir 能递归读取 .md/.txt 建索引，search 命中主题最相关的文档
# 设计：临时目录写两个不同主题文档，检索其中一个主题的关键词，断言返回的 source 指向对应文件
def test_store_load_and_search(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text(
        "The quick brown fox jumps over the lazy dog.", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text(
        "Machine learning is a field of artificial intelligence.", encoding="utf-8"
    )
    store = RagStore(chunk_size=800, chunk_overlap=100)
    n = store.load_dir(tmp_path)
    assert n == 2
    results = store.search("machine learning", top_k=1)
    assert results
    assert results[0].source == str(tmp_path / "b.md")


# 功能：验证空知识库目录下 load_dir 返回 0 且 search 返回空列表，不抛异常
# 设计：指向空目录，断言索引为空、检索安全降级为空结果
def test_store_empty_dir(tmp_path: Path) -> None:
    store = RagStore()
    assert store.load_dir(tmp_path) == 0
    assert store.search("anything") == []


# 功能：验证 RagSearchTool.invoke 返回带来源路径的格式化文本，无匹配时给出友好提示
# 设计：命中时断言 content 含来源文件名与原文片段；空 store 时断言返回 "No matching chunks" 提示
async def test_rag_search_tool_invoke(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text(
        "Deploy steps: build, test, release.", encoding="utf-8"
    )
    store = RagStore(chunk_size=800, chunk_overlap=100)
    store.load_dir(tmp_path)
    tool = RagSearchTool(store, default_top_k=3)

    result = await tool.invoke({"query": "deploy steps"})
    assert not result.is_error
    assert "notes.md" in result.content
    assert "Deploy steps" in result.content

    empty_result = await RagSearchTool(RagStore(), default_top_k=3).invoke(
        {"query": "nothing"}
    )
    assert "No matching chunks" in empty_result.content
