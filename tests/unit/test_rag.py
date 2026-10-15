from __future__ import annotations

import json
from pathlib import Path

import httpx

from kama_claude.core.rag.chunk import (
    _split_chunks,
    parse_frontmatter,
    split_semantic,
    split_with_context,
    strip_frontmatter,
)
from kama_claude.core.rag.index import Bm25Index, HybridIndex, ZhipuEmbeddingIndex, _tokenize
from kama_claude.core.rag.rerank import Reranker
from kama_claude.core.rag.store import RagStore
from kama_claude.core.rag.tool import RagSearchTool


# 固定偏好的 fake reranker：命中偏好词给 1.0，否则 0.0，用于验证精排重排
class _FakeReranker(Reranker):
    def __init__(self, preferred: str) -> None:
        self._preferred = preferred

    async def rerank(self, query: str, texts: list[str]) -> list[float]:
        return [1.0 if self._preferred in t else 0.0 for t in texts]


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


# 功能：验证语义切片把标题与紧随的短段落合并进同一块
# 设计：标题与段落各远小于 size，断言边界落在标题/段落之间而非硬切中间
def test_split_semantic_heading_merges_paragraph() -> None:
    chunks = split_semantic("# 部署\n\n运行 kama-core。", size=100, overlap=0)
    assert chunks == ["# 部署\n运行 kama-core。"]


# 功能：验证超长段落按句末标点切分，而非在句子中间硬切
# 设计：两句各 7 字符，size=10 只够装一句，断言产出两个完整句子块
def test_split_semantic_sentence_boundary() -> None:
    chunks = split_semantic("这是第一句话。这是第二句话。", size=10, overlap=0)
    assert chunks == ["这是第一句话。", "这是第二句话。"]


# 功能：验证语义合并路径也产生 overlap，相邻块共享上一块的尾部上下文
# 设计：两句各 7 字符、size=12、overlap=3，断言第二块以第一块尾部 3 字符开头，
#       证明 overlap 不再局限于 _split_chunks 硬切回退，合并阶段同样生效
def test_split_semantic_merge_overlap() -> None:
    chunks = split_semantic("这是第一句话。这是第二句话。", size=12, overlap=3)
    assert len(chunks) == 2
    assert chunks[0] == "这是第一句话。"
    assert chunks[1].startswith("句话。")
    assert chunks[1].endswith("这是第二句话。")


# 功能：验证代码块内的标点/空格不触发句子切分，整体作为一个单元
# 设计：含小数（"1.5"）的代码块在 size 足够时保持整块，排除按句号误拆
def test_split_semantic_code_not_sentence_split() -> None:
    text = "```\nx = 1.5\ny = 2.5\n```"
    chunks = split_semantic(text, size=1000, overlap=0)
    assert len(chunks) == 1
    assert "1.5" in chunks[0] and "2.5" in chunks[0]


# 功能：验证 split_with_context 为中段切片补所属标题，且块首已是标题时不重复前置
# 设计：首块已被标题占据时不加前缀，超长段落切出的后续块断言以所属标题开头且不重复叠加
def test_split_with_context_attaches_heading() -> None:
    body = "。".join(f"第{i}条说明" for i in range(40))
    pairs = split_with_context(f"# 部署手册\n\n## 回滚步骤\n\n{body}", size=40, overlap=0)
    assert pairs[0][0].startswith("# 部署手册")
    tail = [text for text, heading in pairs if heading == "## 回滚步骤"]
    assert tail and all(t.startswith("## 回滚步骤") for t in tail)
    assert all(t.count("## 回滚步骤") == 1 for t in tail)  # 前缀只加一次


# 功能：验证 parse_frontmatter 解析标量与 [a, b] 列表两种取值写法
# 设计：混合类型断言类型推断结果，列表项去引号去空格，无 frontmatter 返回空
def test_parse_frontmatter_scalar_and_list() -> None:
    meta = parse_frontmatter(
        "---\ndepartment: 研发\npriority: 2\nflag: true\ntags: [ml, 'nlp']\n---\n# 标题"
    )
    assert meta == {
        "department": "研发",
        "priority": 2,
        "flag": True,
        "tags": ["ml", "nlp"],
    }
    assert parse_frontmatter("# 无 frontmatter") == {}


# 功能：验证 strip_frontmatter 只剥离开头的元数据块，未闭合或无块时原样返回
# 设计：闭合块断言正文不含元数据、含标题；无块与未闭合两种情况断言返回原文
def test_strip_frontmatter() -> None:
    assert strip_frontmatter("---\na: 1\n---\n# 标题\n\n正文").startswith("# 标题")
    assert strip_frontmatter("# 标题\n\n正文") == "# 标题\n\n正文"
    assert strip_frontmatter("---\na: 1\n# 标题") == "---\na: 1\n# 标题"


# 功能：验证 load_dir 能递归读取 .md/.txt 建索引，search 命中主题最相关的文档
# 设计：临时目录写两个不同主题文档，检索其中一个主题的关键词，断言返回的 source 指向对应文件
async def test_store_load_and_search(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text(
        "The quick brown fox jumps over the lazy dog.", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text(
        "Machine learning is a field of artificial intelligence.", encoding="utf-8"
    )
    store = RagStore(chunk_size=800, chunk_overlap=100)
    n = await store.load_dir(tmp_path)
    assert n == 2
    results = await store.search("machine learning", top_k=1)
    assert results
    assert results[0].source == str(tmp_path / "b.md")


# 功能：验证空知识库目录下 load_dir 返回 0 且 search 返回空列表，不抛异常
# 设计：指向空目录，断言索引为空、检索安全降级为空结果
async def test_store_empty_dir(tmp_path: Path) -> None:
    store = RagStore()
    assert await store.load_dir(tmp_path) == 0
    assert await store.search("anything") == []


# 功能：验证 load_dir 增量更新：变更文档重切、删除文档自动丢弃、未变文档保持稳定
# 设计：用等长改写（mtime 可能不变）验证内容 hash 而非时间戳作为变更依据，改一个删一个后断言最终 chunk 正确
async def test_store_incremental_rebuild(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# A\n\nalpha content", encoding="utf-8")
    (tmp_path / "b.md").write_text("# B\n\nbeta content", encoding="utf-8")
    store = RagStore(chunk_size=200, chunk_overlap=0)
    await store.load_dir(tmp_path)

    # 改 a（等长改写，验证内容 hash 检测）、删 b，再加载
    (tmp_path / "a.md").write_text("# A\n\nalpha updated", encoding="utf-8")
    (tmp_path / "b.md").unlink()
    await store.load_dir(tmp_path)

    sources = {c.source for c in store._chunks}
    assert str(tmp_path / "b.md") not in sources
    assert any("updated" in c.text for c in store._chunks)
    for c in store._chunks:
        assert c.doc_id and c.content_hash


# 功能：验证内容未变（仅重写/touch）时复用旧 chunk，不产生重复切分或 hash 变化
# 设计：重写相同内容后 reload，断言 chunk 文本与 content_hash 与首次加载完全一致
async def test_store_touch_reuses_chunks(tmp_path: Path) -> None:
    f = tmp_path / "a.md"
    f.write_text("# A\n\nalpha content", encoding="utf-8")
    store = RagStore(chunk_size=200, chunk_overlap=0)
    await store.load_dir(tmp_path)
    first = [(c.text, c.content_hash) for c in store._chunks]

    f.write_text("# A\n\nalpha content", encoding="utf-8")  # 同内容 → 应复用而非重切
    await store.load_dir(tmp_path)
    second = [(c.text, c.content_hash) for c in store._chunks]

    assert first == second


# 功能：验证 RagSearchTool.invoke 返回带来源路径的格式化文本，无匹配时给出友好提示
# 设计：命中时断言 content 含来源文件名与原文片段；空 store 时断言返回 "No matching chunks" 提示
async def test_rag_search_tool_invoke(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("Deploy steps: build, test, release.", encoding="utf-8")
    store = RagStore(chunk_size=800, chunk_overlap=100)
    await store.load_dir(tmp_path)
    tool = RagSearchTool(store, default_top_k=3)

    result = await tool.invoke({"query": "deploy steps"})
    assert not result.is_error
    assert "notes.md" in result.content
    assert "Deploy steps" in result.content

    empty_result = await RagSearchTool(RagStore(), default_top_k=3).invoke({"query": "nothing"})
    assert "No matching chunks" in empty_result.content


# 功能：验证命中结果带上"来源 > 章节"定位信息，便于模型判断片段归属
# 设计：带标题的文档检索命中后断言 content 含标题行；无标题时退化为仅来源，不产生空章节目录
async def test_rag_search_tool_reports_heading(tmp_path: Path) -> None:
    (tmp_path / "ops.md").write_text(
        "# 运维手册\n\n## 回滚步骤\n\n先停止流量再回滚版本。", encoding="utf-8"
    )
    store = RagStore(chunk_size=800, chunk_overlap=0)
    await store.load_dir(tmp_path)
    result = await RagSearchTool(store, default_top_k=3).invoke({"query": "回滚"})
    assert not result.is_error
    assert "ops.md" in result.content
    assert "## 回滚步骤" in result.content


# 功能：验证检索异常被转换为 ToolResult 错误而非冒泡中断 agent 循环
# 设计：注入一个 search 必抛的 store，断言返回 is_error 且 error_type=runtime_error，
#       锁定"知识库故障不应炸掉整轮 run"这一降级契约
async def test_rag_search_tool_swallows_search_error() -> None:
    class _BoomStore(RagStore):
        async def search(self, query, top_k=3, filter=None):  # type: ignore[override]
            raise RuntimeError("qdrant down")

    result = await RagSearchTool(_BoomStore(), default_top_k=3).invoke({"query": "x"})
    assert result.is_error
    assert result.error_type == "runtime_error"
    assert "qdrant down" in result.content


# 功能：验证 filter 传非 dict 时被规整为不过滤，检索照常返回结果
# 设计：LLM 偶发把 filter 传成字符串，断言工具不报错且能返回命中，避免参数畸形导致空手而归
async def test_rag_search_tool_tolerates_bad_filter(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# A\n\nalpha content", encoding="utf-8")
    store = RagStore(chunk_size=200, chunk_overlap=0)
    await store.load_dir(tmp_path)
    result = await RagSearchTool(store, default_top_k=3).invoke(
        {"query": "alpha", "filter": "department"}
    )
    assert not result.is_error
    assert "alpha" in result.content


# 功能：验证 Zhipu 索引分批调用 API、按 index 排序、按余弦相似度命中
# 设计：用 MockTransport 返回确定性向量（含 machine 的文本给 [1,0]，其余 [0,1]），
# 以 batch_size=2 覆盖分批路径，断言 top-1 命中 machine 文档
async def test_zhipu_index_embed_and_search() -> None:
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        inputs = body["input"]
        calls.append(inputs)
        data = []
        for idx, text in enumerate(inputs):
            vec = [1.0, 0.0] if "machine" in text.lower() else [0.0, 1.0]
            data.append({"object": "embedding", "index": idx, "embedding": vec})
        return httpx.Response(200, json={"object": "list", "data": data, "model": "embedding-3"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://test.local"
    )
    index = ZhipuEmbeddingIndex("k", dimensions=2, batch_size=2, client=client)
    await index.build(
        ["machine learning notes", "cooking recipe", "deep learning"], ["0", "1", "2"], "fp"
    )
    assert len(calls) == 2  # 3 条文本按 batch_size=2 分两批
    scores = await index.search("machine learning", top_k=1)
    assert scores == [(0, 1.0)]


# 功能：验证向量缓存按 chunk 内容 hash 复用：内容未变不重复请求，仅新 chunk 触发请求
# 设计：用 MockTransport 计数，同一内容二次构建不新增调用；换掉一个 chunk 后只补请求新 chunk
async def test_zhipu_index_cache(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        data = [
            {"object": "embedding", "index": i, "embedding": [1.0]}
            for i in range(len(body["input"]))
        ]
        return httpx.Response(200, json={"object": "list", "data": data, "model": "embedding-3"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://test.local"
    )
    cache_path = tmp_path / "cache.json"

    index = ZhipuEmbeddingIndex(
        "k", dimensions=1, cache_path=cache_path, cache_enabled=True, client=client
    )
    await index.build(["a", "b"], ["0", "1"], "fp1")
    assert calls == 1

    index2 = ZhipuEmbeddingIndex(
        "k", dimensions=1, cache_path=cache_path, cache_enabled=True, client=client
    )
    await index2.build(["a", "b"], ["0", "1"], "fp1")
    assert calls == 1  # 内容未变，命中缓存

    index3 = ZhipuEmbeddingIndex(
        "k", dimensions=1, cache_path=cache_path, cache_enabled=True, client=client
    )
    await index3.build(["a", "c"], ["0", "1"], "fp2")
    assert calls == 2  # 仅 "c" 为新 chunk，只补一次请求


# 功能：验证带 reranker 的 search 先取候选集再按精排分数重排，返回顺序被 reranker 改变
# 设计：fake reranker 给含 "cooking" 的 chunk 最高分，断言 search 首项指向 cooking 文档，
#       即使 BM25 会把更短的 "machine learning" 文档排在前面，证明精排阶段生效
async def test_store_search_with_reranker(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("machine learning notes", encoding="utf-8")
    (tmp_path / "b.txt").write_text("cooking recipe about machine learning", encoding="utf-8")
    store = RagStore(reranker=_FakeReranker("cooking"), rerank_depth=10)
    await store.load_dir(tmp_path)
    results = await store.search("machine learning", top_k=1)
    assert results
    assert results[0].source == str(tmp_path / "b.txt")


# 功能：验证 Bm25Index 作为本地词法回退后端仍能检索
# 设计：直接对 Bm25Index 建索引并检索，确认与 RagStore 解耦后行为不变
async def test_bm25_index_search() -> None:
    index = Bm25Index()
    await index.build(["machine learning notes", "cooking recipe"], ["0", "1"], "fp")
    scores = await index.search("machine learning", top_k=1)
    assert len(scores) == 1
    assert scores[0][0] == 0
    assert scores[0][1] > 0.0


# 功能：验证 BM25 倒排表正确构建：df 计数、posting 表含 (doc_idx, tf) 且按 doc 升序
# 设计：两篇文档共享词项，断言 df 计数、posting 长度与 tf 值，确认倒排结构正确
async def test_bm25_index_postings_built() -> None:
    index = Bm25Index()
    await index.build(["alpha alpha beta", "beta gamma"], ["0", "1"], "fp")
    assert index._df["alpha"] == 1
    assert index._df["beta"] == 2
    assert index._postings["alpha"] == [(0, 2)]
    assert index._postings["beta"] == [(0, 1), (1, 1)]
    assert index._postings["gamma"] == [(1, 1)]


# 功能：验证倒排表检索与原全量扫描 BM25 结果一致（排序与分数均等价）
# 设计：多篇共享词项/不同长度的文档，用倒排表检索后与手算 BM25 分数比对，
#       证明倒排 TAAT 累加与逐文档扫描打分严格等价
async def test_bm25_index_inverted_equals_bruteforce() -> None:
    texts = [
        "machine learning machine learning",
        "machine learning notes",
        "cooking recipe",
    ]
    index = Bm25Index()
    await index.build(texts, ["0", "1", "2"], "fp")
    got = await index.search("machine learning", top_k=10)

    # 手算：逐文档累加 BM25 分数（文档长度 = 总 token 数），与倒排表结果逐项对齐
    def _score(doc: str) -> float:
        counts: dict[str, int] = {}
        for tok in _tokenize(doc):
            counts[tok] = counts.get(tok, 0) + 1
        dl = sum(counts.values()) or 1
        s = 0.0
        for t in set(_tokenize("machine learning")):
            f = counts.get(t)
            if not f:
                continue
            num = f * (index._k1 + 1)
            den = f + index._k1 * (1 - index._b + index._b * dl / index._avgdl)
            s += index._idf(t) * num / den
        return s

    expected = sorted(
        [(i, _score(t)) for i, t in enumerate(texts) if _score(t) > 0.0],
        key=lambda x: (-x[1], x[0]),
    )
    assert got == expected


# 功能：验证倒排表检索遵守 allowed 白名单过滤，只返回白名单内命中的文档
# 设计：三篇文档只允许下标 {0, 2}，断言结果不含下标 1，证明过滤贯穿倒排路径
async def test_bm25_index_inverted_allowed() -> None:
    index = Bm25Index()
    await index.build(
        ["machine learning", "machine learning", "machine learning"],
        ["0", "1", "2"],
        "fp",
    )
    scores = await index.search("machine learning", top_k=10, allowed={0, 2})
    got = {i for i, _ in scores}
    assert got == {0, 2}


# 功能：验证 HybridIndex 用 RRF 融合词法 + 语义，让两边命中的 chunk 都进入 top-k
# 设计：词法只命中 "apple" 的文档、语义只命中 "vector" 的文档，断言 RRF 合并后两者都排在无关文档前，
#       证明混合检索同时保留字面召回与语义泛化两条信号
async def test_hybrid_index_rrf_fusion() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        data = []
        for idx, text in enumerate(body["input"]):
            vec = [1.0, 0.0] if "vector" in text.lower() else [0.0, 1.0]
            data.append({"object": "embedding", "index": idx, "embedding": vec})
        return httpx.Response(200, json={"object": "list", "data": data, "model": "embedding-3"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://test.local"
    )
    dense = ZhipuEmbeddingIndex("k", dimensions=2, client=client)
    index = HybridIndex(Bm25Index(), dense, depth=10)

    texts = [
        "word2vec embeddings are dense vectors",  # 语义命中（含 vector）
        "apple banana orange",  # 词法命中（含 apple）
        "completely unrelated topic",  # 两者都不命中
    ]
    await index.build(texts, ["0", "1", "2"], "fp")
    scores = await index.search("apple vector", top_k=2)
    got = {i for i, _ in scores}
    assert 0 in got and 1 in got
    assert 2 not in got


# 功能：验证 load_dir 提取各文档标题并聚合为全局主题清单（manifest）
# 设计：两个带标题的 md 文档，断言 manifest 含两者标题、去重且顺序稳定，空标题文档不污染清单
async def test_store_manifest_topics(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# 部署手册\n\n运行步骤", encoding="utf-8")
    (tmp_path / "b.md").write_text("# 配置优先级\n\n五层优先级", encoding="utf-8")
    store = RagStore(chunk_size=200, chunk_overlap=0)
    await store.load_dir(tmp_path)
    manifest = store.manifest()
    assert "部署手册" in manifest
    assert "配置优先级" in manifest
    # 标题去重：重复标题只出现一次
    assert manifest.count("部署手册") == 1


# 功能：验证 RagSearchTool 构造时把知识库主题清单拼进 description（层 2）
# 设计：加载带标题文档后构造工具，断言 description 含 manifest 文本，帮助 Agent 判断检索边界
async def test_rag_search_tool_description_has_manifest(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# 部署手册\n\n运行步骤", encoding="utf-8")
    store = RagStore(chunk_size=200, chunk_overlap=0)
    await store.load_dir(tmp_path)
    tool = RagSearchTool(store, default_top_k=3)
    assert "部署手册" in tool.description


# 功能：验证 ExecutionContext 系统提示注入 RAG 检索指令与主题清单（层 1 + 层 2）
# 设计：设置 rag_instruction 与 rag_manifest 后调用 system_prompt，断言两者都出现在输出中
def test_context_system_prompt_injects_rag() -> None:
    from kama_claude.core.context import ExecutionContext

    ctx = ExecutionContext(
        run_id="r",
        goal="g",
        max_steps=1,
        rag_instruction="必须调用 rag_search",
        rag_manifest="知识库当前覆盖主题：部署",
    )
    prompt = ctx.system_prompt("base")
    assert "必须调用 rag_search" not in prompt
    assert "知识库当前覆盖主题：部署" not in prompt
    assert "必须调用 rag_search" in str(ctx.model_messages())
    assert "知识库当前覆盖主题：部署" in str(ctx.model_messages())


# 功能：验证 frontmatter 解析出 key: value 元数据，并按标量类型推断；无 frontmatter 返回空
# 设计：字符串/整数/浮点/布尔四类值断言类型被正确推断，普通文档返回空、非法行被忽略
def test_parse_frontmatter() -> None:
    from kama_claude.core.rag.chunk import parse_frontmatter

    text = "---\ndepartment: 研发\nupdated: 2026-09\ncount: 10\nratio: 0.5\nenabled: true\n---\n# 正文\n"
    assert parse_frontmatter(text) == {
        "department": "研发",
        "updated": "2026-09",
        "count": 10,
        "ratio": 0.5,
        "enabled": True,
    }
    assert parse_frontmatter("# 无 frontmatter\n正文") == {}


# 功能：验证 load_dir 填充结构化元数据（source/updated_at/frontmatter）
# 设计：写带 frontmatter 的文档，断言 chunk.metadata 含自动 source 与显式 department
async def test_store_metadata_populated(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(
        "---\ndepartment: 研发\n---\n# 部署手册\n\n运行步骤", encoding="utf-8"
    )
    store = RagStore(chunk_size=200, chunk_overlap=0)
    await store.load_dir(tmp_path)
    assert store._chunks
    meta = store._chunks[0].metadata
    assert meta["department"] == "研发"
    assert meta["source"] == "a.md"
    assert "updated_at" in meta


# 功能：验证结构化 filter 过滤：按 department 字段只返回命中部门的 chunk
# 设计：两个不同部门文档，用 filter={"department": "研发"} 检索，断言结果只含研发文档
async def test_store_search_metadata_filter(tmp_path: Path) -> None:
    (tmp_path / "rd.md").write_text(
        "---\ndepartment: 研发\n---\n# 部署手册\n\n运行步骤", encoding="utf-8"
    )
    (tmp_path / "fin.md").write_text(
        "---\ndepartment: 财务\n---\n# 报销流程\n\n提交单据", encoding="utf-8"
    )
    store = RagStore(chunk_size=200, chunk_overlap=0)
    await store.load_dir(tmp_path)
    results = await store.search("步骤", filter={"department": "研发"})
    assert results
    assert all(c.metadata["department"] == "研发" for c in results)
    assert {c.source for c in results} == {str(tmp_path / "rd.md")}


# 功能：验证 metadata_manifest 枚举每个字段及其取值，供 LLM 生成合法过滤条件
# 设计：两个部门文档，断言词汇表含 department 字段与两个取值
async def test_store_metadata_manifest(tmp_path: Path) -> None:
    (tmp_path / "rd.md").write_text("---\ndepartment: 研发\n---\n# 部署手册", encoding="utf-8")
    (tmp_path / "fin.md").write_text("---\ndepartment: 财务\n---\n# 报销流程", encoding="utf-8")
    store = RagStore(chunk_size=200, chunk_overlap=0)
    await store.load_dir(tmp_path)
    mm = store.metadata_manifest()
    assert "department" in mm
    assert "研发" in mm
    assert "财务" in mm


# 功能：验证工具 description 同时含主题清单与元数据词汇表，帮助 LLM 决策过滤条件
# 设计：带 frontmatter 文档，断言 description 含 manifest 与 metadata_manifest 两段
async def test_rag_search_tool_description_has_metadata(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(
        "---\ndepartment: 研发\n---\n# 部署手册\n\n运行步骤", encoding="utf-8"
    )
    store = RagStore(chunk_size=200, chunk_overlap=0)
    await store.load_dir(tmp_path)
    tool = RagSearchTool(store, default_top_k=3)
    assert "部署手册" in tool.description
    assert "department" in tool.description
    assert "研发" in tool.description


# 功能：验证 filter 列表值按任一匹配（MatchAny 语义），命中多个部门的 chunk
# 设计：三个部门文档，filter 传两个部门，断言返回两者、排除第三个
async def test_store_search_filter_list_any(tmp_path: Path) -> None:
    for name, dept in (("rd", "研发"), ("fin", "财务"), ("ops", "运维")):
        (tmp_path / f"{name}.md").write_text(
            f"---\ndepartment: {dept}\n---\n# {dept}手册\n\n{dept}运行步骤", encoding="utf-8"
        )
    store = RagStore(chunk_size=200, chunk_overlap=0)
    await store.load_dir(tmp_path)
    results = await store.search("运行步骤", filter={"department": ["研发", "财务"]})
    assert results
    assert {c.metadata["department"] for c in results} == {"研发", "财务"}
    assert all(c.metadata["department"] != "运维" for c in results)


# 功能：验证 filter 支持字符串范围条件（updated_at 的 gte/lte），按字典序过滤
# 设计：不同 mtime 无法稳定控制，改用 frontmatter 的 year 字段做数值范围，
#       断言只返回 year 落在范围内的 chunk
async def test_store_search_filter_range(tmp_path: Path) -> None:
    for name, year in (("a", "2024"), ("b", "2025"), ("c", "2026")):
        (tmp_path / f"{name}.md").write_text(
            f"---\nyear: {year}\n---\n# 文档{name}\n\n内容", encoding="utf-8"
        )
    store = RagStore(chunk_size=200, chunk_overlap=0)
    await store.load_dir(tmp_path)
    results = await store.search("内容", filter={"year": {"gte": 2025, "lte": 2026}})
    assert results
    assert {c.metadata["year"] for c in results} == {2025, 2026}


# 功能：验证倒排索引与线性扫描在过滤结果上等价
# 设计：多字段多值文档，分别用命中/未命中/范围过滤检索，断言结果稳定且无越界
async def test_store_inverted_index_consistency(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(
        "---\ndepartment: 研发\npriority: 1\n---\n# A\n\nalpha content", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text(
        "---\ndepartment: 财务\npriority: 2\n---\n# B\n\nbeta content", encoding="utf-8"
    )
    store = RagStore(chunk_size=200, chunk_overlap=0)
    await store.load_dir(tmp_path)
    # 倒排索引已构建
    assert store._inverted
    by_dept = await store.search("content", filter={"department": "研发"})
    assert {c.doc_id for c in by_dept} == {"a.md"}
    by_priority = await store.search("content", filter={"priority": {"gte": 2}})
    assert {c.doc_id for c in by_priority} == {"b.md"}


# 功能：验证切片为中段段落补上所属标题前缀，使孤立片段可判断归属
# 设计：文档含两个章节且第二节超长，断言后续块以所属标题开头而非直接是正文，
#       证明标题上下文注入在合并阶段生效（而不只是记录 heading 字段）
async def test_store_chunk_heading_context(tmp_path: Path) -> None:
    body = "。".join(f"第{i}条说明" for i in range(40))
    (tmp_path / "a.md").write_text(f"# 部署手册\n\n## 回滚步骤\n\n{body}", encoding="utf-8")
    store = RagStore(chunk_size=60, chunk_overlap=0)
    await store.load_dir(tmp_path)
    headings = {c.heading for c in store._chunks}
    assert "## 回滚步骤" in headings
    # 不在块首的切片已被前置所属标题
    assert any(c.text.startswith("## 回滚步骤") for c in store._chunks)


# 功能：验证 reload 能感知新增文档并重建索引，且内容未变时不重复重建
# 设计：首次 load 后直接 reload 断言签名字符串不变（跳过重建），再新增文件后 reload
#       断言新文档可被检索到，覆盖 daemon 长驻场景下的知识库热更新
async def test_store_reload_picks_up_new_files(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# A\n\nalpha content", encoding="utf-8")
    store = RagStore(chunk_size=200, chunk_overlap=0)
    await store.load_dir(tmp_path)
    sig_before = store._docs_signature
    assert await store.reload() == len(store._chunks)
    assert store._docs_signature == sig_before  # 无变化 → 不重建

    (tmp_path / "b.md").write_text("# B\n\nbeta content", encoding="utf-8")
    n = await store.reload()
    assert n == len(store._chunks)
    assert {c.doc_id for c in store._chunks} == {"a.md", "b.md"}
    results = await store.search("beta", top_k=3)
    assert {c.doc_id for c in results} == {"b.md"}


# 功能：验证元数据词汇表对高基数字段降级为"字段名 + 计数"，不再逐条枚举取值
# 设计：写入 25 个文档（超过 _ENUM_FIELD_LIMIT），断言 source 字段被折叠为计数说明，
#       避免成百上千条文件路径灌进每轮工具描述推高 token 成本
async def test_store_metadata_manifest_caps_high_cardinality(tmp_path: Path) -> None:
    for i in range(25):
        (tmp_path / f"d{i}.md").write_text(
            f"---\ndepartment: 研发\n---\n# 文档{i}\n\n内容", encoding="utf-8"
        )
    store = RagStore(chunk_size=200, chunk_overlap=0)
    await store.load_dir(tmp_path)
    mm = store.metadata_manifest()
    assert "department" in mm and "研发" in mm
    assert "source" in mm
    assert "共 25 个取值" in mm
    assert "d24.md" not in mm  # 高基数字段不再枚举具体取值


# 功能：验证 filter 传入非 dict 时被安全忽略，检索照常返回结果而不抛错
# 设计：LLM 偶发把 filter 传成字符串/数组，此处断言不抛异常且退化为不过滤，
#       保证畸形参数不会中断整轮 agent 循环
async def test_store_search_ignores_non_dict_filter(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# A\n\nalpha content", encoding="utf-8")
    store = RagStore(chunk_size=200, chunk_overlap=0)
    await store.load_dir(tmp_path)
    for bad in ("department", ["研发"], 123):
        results = await store.search("alpha", filter=bad)  # type: ignore[arg-type]
        assert {c.doc_id for c in results} == {"a.md"}


# 功能：验证 frontmatter 不进入检索正文与主题清单，元数据仅存于 chunk.metadata
# 设计：含 frontmatter 与标题的文档，断言 chunk.text 不含 "department:"、主题取到标题
#       而非元数据行；此前元数据块会混进每个 chunk 并污染主题清单
async def test_store_frontmatter_excluded_from_chunks(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(
        "---\ndepartment: 研发\ntags: [ml, nlp]\n---\n# 部署手册\n\n运行步骤内容",
        encoding="utf-8",
    )
    store = RagStore(chunk_size=200, chunk_overlap=0)
    await store.load_dir(tmp_path)
    assert all("department:" not in c.text and "---" not in c.text for c in store._chunks)
    assert "运行步骤内容" in "".join(c.text for c in store._chunks)
    assert store._topics == ["部署手册"]
    assert store._chunks[0].metadata["tags"] == ["ml", "nlp"]


# 功能：验证 frontmatter 的列表写法被解析为列表，从而支持按标签包含语义过滤
# 设计：tags 用 [ml, nlp] 写法，filter 传单个标签应命中；断言列表未被压成整串
async def test_store_frontmatter_list_metadata_filter(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text(
        "---\ntags: [ml, nlp]\n---\n# A\n\nalpha content", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text("---\ntags: [ops]\n---\n# B\n\nalpha content", encoding="utf-8")
    store = RagStore(chunk_size=200, chunk_overlap=0)
    await store.load_dir(tmp_path)
    results = await store.search("alpha", filter={"tags": "ml"})
    assert {c.doc_id for c in results} == {"a.md"}
