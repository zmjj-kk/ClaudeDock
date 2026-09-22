from __future__ import annotations

from kama_claude.core.llm.types import LlmResponse
from kama_claude.core.rag.index import rrf_fuse
from kama_claude.core.rag.query import LlmQueryProcessor, optimize_query, parse_query_list
from kama_claude.core.rag.store import RagStore


# 功能：验证 optimize_query 把连续空白压缩为单个空格并去掉首尾空白
# 设计：多空格/制表符/换行混合输入，断言归一化为单空格分隔的紧凑字符串
def test_optimize_query_collapses_whitespace() -> None:
    assert optimize_query("  配置   优先级\t\n五层  ") == "配置 优先级 五层"


# 功能：验证 parse_query_list 解析 JSON 字符串数组，剥离围栏与语言标签
# 设计：围栏包裹的 JSON 数组，断言逐项提取且保留字符串内容
def test_parse_query_list_json_array() -> None:
    text = '```json\n["配置 优先级", "config priority", "五层优先级"]\n```'
    assert parse_query_list(text) == ["配置 优先级", "config priority", "五层优先级"]


# 功能：验证 parse_query_list 退化逐行解析，去掉项目符号与编号
# 设计：带 "- " 与 "1." 前缀的多行文本，断言前缀被剥离、空行被忽略
def test_parse_query_list_lines() -> None:
    text = "- 配置优先级\n1. config priority\n\n- 五层配置\n"
    assert parse_query_list(text) == ["配置优先级", "config priority", "五层配置"]


# 功能：验证 rrf_fuse 按排名融合多路召回，让多路都命中的下标排在最前
# 设计：两路排名各自覆盖不同下标，交集下标应获得更高融合分而排前
def test_rrf_fuse_ranks_by_reciprocal() -> None:
    fused = rrf_fuse([[(0, 1.0), (1, 0.9)], [(1, 1.0), (2, 0.9)]])
    assert fused[0][0] == 1  # 两路都命中，融合分最高
    got = {i for i, _ in fused}
    assert got == {0, 1, 2}


# 功能：验证 LlmQueryProcessor 把原始查询置于首位并追加 LLM 拓展，去重限幅
# 设计：剧本 provider 返回含重复与超量项的 JSON 数组，断言首项为原始查询、结果去重
async def test_llm_query_processor_transforms() -> None:
    class _Provider:
        async def chat(self, messages, tool_schemas, bus, run_id, *, step=0, system=None):
            return LlmResponse(
                stop_reason="end_turn",
                text='["配置 优先级", "config priority", "配置优先级"]',
            )

    proc = LlmQueryProcessor(_Provider(), expand_n=3)
    out = await proc.transform("  配置优先级  ")
    assert out[0] == "配置优先级"  # 首项为规范化后的原始查询
    assert "config priority" in out
    assert out.count("配置优先级") == 1  # 与原始查询重复的拓展被去重


# 功能：验证 LlmQueryProcessor 在 provider 抛异常时安全回退为仅原始查询
# 设计：provider 抛 RuntimeError，断言不向上传播、仅返回原始查询
async def test_llm_query_processor_fallback_on_error() -> None:
    class _Provider:
        async def chat(self, messages, tool_schemas, bus, run_id, *, step=0, system=None):
            raise RuntimeError("boom")

    proc = LlmQueryProcessor(_Provider(), expand_n=3)
    assert await proc.transform("配置优先级") == ["配置优先级"]


# 功能：验证配置了 query_processor 的 search 对多查询做 RRF 融合，召回更全
# 设计：确定性 processor 返回两条互补查询，断言融合后命中的 chunk 覆盖两个文档
async def test_store_search_query_processor_fusion(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("config priority layers", encoding="utf-8")
    (tmp_path / "b.txt").write_text("配置 优先级 五层", encoding="utf-8")

    class _Processor:
        async def transform(self, query: str) -> list[str]:
            return [query, "config priority", "配置优先级"]

    store = RagStore(query_processor=_Processor(), query_depth=10)
    await store.load_dir(tmp_path)
    results = await store.search("配置优先级", top_k=2)
    got = {c.doc_id for c in results}
    assert got == {"a.txt", "b.txt"}
