# RAG 检索升级：TF-IDF → BM25 + 混合检索

本文记录本次对 KamaClaude 本地知识库检索（`src/kama_claude/core/rag/`）的改进，含关键代码示例与验证结果。

## 一、概述

| 维度 | 升级前 | 升级后 |
|------|--------|--------|
| 词法检索 | TF-IDF（归一化向量 + 余弦） | **BM25**（词频饱和 + 文档长度归一化） |
| 语义检索 | 智谱 embedding-3（纯稠密余弦） | 保留 |
| 整体检索 | 二选一（配 key 走 embedding，否则 TF-IDF） | **混合检索**（BM25 + embedding，RRF 融合） |
| 零依赖回退 | TF-IDF | **BM25** |

核心改动都发生在 `RagIndex` 这个抽象接口内部——`RagStore`、`RagSearchTool` 及所有调用方无需感知。新增的 `HybridIndex` 本身也是一个 `RagIndex` 实现，内部组合「词法 + 语义」两个子检索器。

---

## 二、词法检索升级：`Bm25Index` 替换 `TfidfIndex`

### 动机

TF-IDF 用「归一化词频向量 + 余弦」打分，有两个已知缺陷：

1. **词频不饱和**：同一个词出现 10 次和出现 100 次，余弦向量里线性放大，长文虚高；
2. **文档长度处理粗糙**：靠向量归一化近似，不如 BM25 的显式长度归一化。

BM25 是信息检索的「词法标杆」，用两个参数显式解决这两点。

### BM25 打分公式

```
score(d, q) = Σ_{t∈q}  IDF(t) · ─────────────────────────────
                                   f(t,d) + k1·(1 − b + b·|d|/avgdl)

IDF(t) = ln( 1 + (N − df(t) + 0.5) / (df(t) + 0.5) )

k1 = 1.5   词频饱和参数（默认）
b  = 0.75  文档长度归一化强度（默认）
```

- `k1`：词频超过一定次数后收益递减；
- `b`：`b=0` 不归一化长度，`b=1` 完全按 `|d|/avgdl` 归一化。

### 关键代码

```python
class Bm25Index(RagIndex):
    # 初始化 BM25 参数：k1 词频饱和、b 文档长度归一化
    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self._k1 = k1
        self._b = b
        self._n = 0
        self._df: dict[str, int] = {}
        self._doc_tf: list[dict[str, int]] = []
        self._doc_len: list[int] = []
        self._avgdl = 0.0

    # 统计每篇文档词频、文档长度与全局 df，并计算平均文档长度
    async def build(self, texts: list[str], fingerprint: str) -> None:
        del fingerprint
        self._n = len(texts)
        self._df = {}
        self._doc_tf = []
        self._doc_len = []
        for text in texts:
            counts: dict[str, int] = {}
            for tok in _tokenize(text):
                counts[tok] = counts.get(tok, 0) + 1
            self._doc_tf.append(counts)
            self._doc_len.append(sum(counts.values()) or 1)
            for tok in counts:
                self._df[tok] = self._df.get(tok, 0) + 1
        self._avgdl = sum(self._doc_len) / self._n if self._n else 0.0

    # BM25 IDF：log(1 + (N - df + 0.5) / (df + 0.5))
    def _idf(self, tok: str) -> float:
        df = self._df.get(tok, 0)
        return math.log(1 + (self._n - df + 0.5) / (df + 0.5))

    # 对每篇文档累加查询词项的 BM25 得分，按分数取 top_k
    async def search(self, query: str, top_k: int) -> list[tuple[int, float]]:
        if self._n == 0 or not query.strip():
            return []
        q_tokens = set(_tokenize(query))
        scores: list[tuple[float, int]] = []
        for i in range(self._n):
            tf = self._doc_tf[i]
            dl = self._doc_len[i]
            s = 0.0
            for t in q_tokens:
                f = tf.get(t)
                if f:
                    num = f * (self._k1 + 1)
                    den = f + self._k1 * (1 - self._b + self._b * dl / self._avgdl)
                    s += self._idf(t) * num / den
            if s > 0.0:
                scores.append((s, i))
        scores.sort(key=lambda x: -x[0])
        return [(i, s) for s, i in scores[:top_k]]
```

分词器 `_tokenize`（英文单词 + 中文 bigram）保持不变，BM25 与原来的 TF-IDF 共用。

---

## 三、混合检索：`HybridIndex` + RRF 融合

### 为什么用 RRF 而不是加权求和

BM25 分数是**无上界正数**（可能 5、30、100），余弦分数在 `[0,1]`。两者尺度完全不同，直接加权求和必须先做分数归一化（min-max 等），而归一化对语料分布敏感、易漂移。

RRF（Reciprocal Rank Fusion，倒数排名融合）只关心「各 ranker 里的**排名**」，天然尺度无关、无需调权，是混合检索的事实标准做法：

```
RRF(d) = Σ_{r ∈ rankers}  1 / (k + rank_r(d))
k = 60   （社区惯例值，几乎不用调）
```

### 关键代码

```python
class HybridIndex(RagIndex):
    # 组合词法与语义两个子检索器，用 RRF 按排名融合
    def __init__(
        self,
        lexical: RagIndex,
        dense: RagIndex,
        *,
        rrf_k: int = 60,
        depth: int = 100,
    ) -> None:
        self._lexical = lexical
        self._dense = dense
        self._rrf_k = rrf_k
        self._depth = depth

    # 同时构建两个子索引（BM25 重建成本低；embedding 命中已有缓存）
    async def build(self, texts: list[str], fingerprint: str) -> None:
        await self._lexical.build(texts, fingerprint)
        await self._dense.build(texts, fingerprint)

    # 分别取 depth 个候选，按 RRF 分数合并后返回 top_k
    async def search(self, query: str, top_k: int) -> list[tuple[int, float]]:
        lex = await self._lexical.search(query, self._depth)
        den = await self._dense.search(query, self._depth)
        fused: dict[int, float] = {}
        for rank, (idx, _s) in enumerate(lex):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (self._rrf_k + rank + 1)
        for rank, (idx, _s) in enumerate(den):
            fused[idx] = fused.get(idx, 0.0) + 1.0 / (self._rrf_k + rank + 1)
        ranked = sorted(fused.items(), key=lambda x: -x[1])
        return [(idx, s) for idx, s in ranked[:top_k]]
```

**分工**：BM25 负责「精确词 / 专有名词 / 代码标识符」的字面召回；embedding 负责「同义 / 改述」的语义泛化。RRF 自动让「两边都排前面的 chunk」胜出。

---

## 四、配置与接线

### `RagConfig` 新增字段

```python
retriever: str = "auto"   # 检索后端：auto/bm25/embedding/hybrid
```

取值校验（TOML 与 env 共用 `_RETRIEVER_CHOICES = ("auto", "bm25", "embedding", "hybrid")`）：

```python
rag_retriever = os.environ.get("KAMA_RAG_RETRIEVER")
if rag_retriever is not None:
    if rag_retriever not in _RETRIEVER_CHOICES:
        raise SystemExit(
            "Config error: KAMA_RAG_RETRIEVER must be one of "
            f"{list(_RETRIEVER_CHOICES)}, got: {rag_retriever!r}"
        )
    config.rag.retriever = rag_retriever
```

### `runner.py` 按 `retriever` 选择后端

```python
async def _get_rag_store(self) -> RagStore:
    store = self._rag_store
    if store is None:
        rag = self._config.rag

        # 构建智谱 embedding 检索后端，供 hybrid / embedding 两种模式复用
        def _dense() -> ZhipuEmbeddingIndex:
            return ZhipuEmbeddingIndex(
                api_key=rag.api_key,
                model=rag.embedding_model,
                base_url=rag.embedding_base_url,
                dimensions=rag.embedding_dimensions,
                batch_size=rag.embedding_batch_size,
                cache_path=Path(rag.knowledge_dir).expanduser() / ".rag_index_cache.json",
                cache_enabled=rag.index_cache,
            )

        # 按 retriever 选后端；auto 时配了 key 走混合检索，否则纯 BM25 零依赖回退
        if rag.retriever == "bm25":
            index: RagIndex = Bm25Index()
        elif rag.retriever == "embedding":
            index = _dense()
        elif rag.retriever == "hybrid":
            index = HybridIndex(Bm25Index(), _dense())
        elif rag.api_key:
            index = HybridIndex(Bm25Index(), _dense())
        else:
            index = Bm25Index()

        store = RagStore(
            chunk_size=rag.chunk_size,
            chunk_overlap=rag.chunk_overlap,
            index=index,
        )
        ...
```

`RagStore` 的缺省索引也从 `TfidfIndex()` 改为 `Bm25Index()`，因此不显式传 `index` 时走纯 BM25 回退。

### 使用方式

```bash
# 环境变量
KAMA_RAG_RETRIEVER=hybrid     # auto / bm25 / embedding / hybrid

# 或 config.toml
[rag]
retriever = "hybrid"
```

- `auto`（默认）：配了 `KAMA_RAG_API_KEY` → 混合检索；没配 → 纯 BM25。
- `bm25`：纯词法，零依赖。
- `embedding`：纯语义（原行为）。
- `hybrid`：BM25 + embedding，RRF 融合（推荐）。

---

## 五、附带修复 1：`rag_search` 权限默认放行

`rag_search` 之前未登记在 `DEFAULT_POLICIES`，被当作「未知工具」走 ASK；而 `kama run` 不订阅/应答权限审批，导致每次调用挂起 60s 超时后拒绝，RAG 实际不可用（这也是实测 142s 慢跑的根因）。

```python
DEFAULT_POLICIES: dict[str, ToolPolicy] = {
    "bash":       ToolPolicy(default=PermissionDecision.ASK),
    "write_file": ToolPolicy(default=PermissionDecision.ASK),
    "read_file":  ToolPolicy(default=PermissionDecision.ALLOW),
    "list_dir":   ToolPolicy(default=PermissionDecision.ALLOW),
    "note_save":  ToolPolicy(default=PermissionDecision.ALLOW),
    "rag_search": ToolPolicy(default=PermissionDecision.ALLOW),   # ← 新增
}
```

修复后 `rag_search` 与 `read_file`/`list_dir` 一样只读放行，`kama run` 实测从 142s 降到 11.9s。

---

## 六、附带修复 2：配置层数文档同步（四层 → 五层）

代码实际是**五层**配置优先级（默认值 → 全局 TOML → 项目本地 TOML → `.env` → 环境变量），但文档写的是「四层」，漏掉了「项目本地 `.kama/config.toml`」。已同步修正：

- `CLAUDE.md` / `AGENT.md` / `docs/architecture.md` / `docs/rag.md`
- `~/.kama/knowledge/architecture.md`（RAG 读取的副本，已删除旧索引缓存待重建）

---

## 七、测试与验证

### 新增回归测试：混合检索 RRF 融合

```python
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

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://test.local")
    dense = ZhipuEmbeddingIndex("k", client=client)
    index = HybridIndex(Bm25Index(), dense, depth=10)

    texts = [
        "word2vec embeddings are dense vectors",   # 语义命中（含 vector）
        "apple banana orange",                      # 词法命中（含 apple）
        "completely unrelated topic",               # 两者都不命中
    ]
    await index.build(texts, "fp")
    scores = await index.search("apple vector", top_k=2)
    got = {i for i, _ in scores}
    assert 0 in got and 1 in got
    assert 2 not in got
```

### 验证结果

```
pytest tests/unit/test_rag.py:                    14 passed（含新增 hybrid RRF 用例）
pytest config + permission + rag:                 51 passed
mypy src:                                         Success（92 files，无类型错误）
ruff（本次改动文件）:                              全干净
端到端:                                           hybrid (BM25 + embedding-3 2048d) 跑通
```

> 注：`config.py` 中存在 10 个升级前就有的 E501 行超长（compaction/mcp 段），与本次改动无关。

---

## 八、增量更新：内容寻址缓存 + 文档关联

为避免每次 run 全量重嵌入，更新策略从「整库一个指纹 + 全量重算」改为「按内容寻址的增量更新」：

1. **文档/Chunk 关联**：`RagChunk` 新增 `doc_id`（相对知识库根目录的路径）与稳定 `id`（`"{doc_id}::{序号}"`），chunk id 自带文档前缀，便于按文档聚合与删除。
2. **内容 hash 权威检测**：每次加载都读取每个文件并计算全文 sha256，与 `.rag_index_manifest.json` 里的历史 hash 比对——未变则复用持久化 chunk（不重新切分），变了才重切，删除的文档自动丢弃。
3. **向量按内容 hash 寻址**：embedding 缓存从「整库指纹 → 向量数组」改为 `{内容hash: 向量}`。未变 chunk 直接命中、零 API 调用，只有新增/变更 chunk 才请求 embedding；陈旧向量在下次写缓存时被清理。

> 早期方案用 `mtime+size` 做第一级粗筛，但实测 WSL2 对「等长、毫秒级」改写不更新 mtime，会漏检。因此最终以**内容 hash 作为唯一变更依据**——读文件本身很廉价，真正昂贵的是 embedding，那部分已由内容寻址缓存规避。

关键代码：`RagStore.load_dir`（`store.py`）、`ZhipuEmbeddingIndex.build`（`index.py`）。

---

## 九、已知限制（未在本次处理）

检索精度仍受**切片粒度**限制：正确内容「五层配置优先级」所在 chunk 同时混装了「核心设计思路」的多条要点，embedding 向量被稀释。这与检索器无关，属 `chunk_size`/分块策略的调优范畴，需要时再处理。
