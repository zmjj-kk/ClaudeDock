# KamaClaude RAG 检索增强设计

> 定位：为 KamaClaude 引入**本地知识库检索增强（RAG）**，让 agent 在回答前先召回项目/领域知识，弥补「静态 context.md 注入」和「上下文压缩」两套机制在"按需取用外部知识"上的空白。
> 首版采用**零外部依赖**的内置 TF-IDF 方案，作为一个普通 `BaseTool`（`rag_search`）接入工具白名单体系；文档末尾给出走 MCP 的替代路径供需要更强语义检索时升级。

---

## 0. 背景与目标

KamaClaude 现有的"知识"来源是两处静态注入：

- **Global/Project Context**：`~/.kama/context.md` 与 `.kama/context.md`，每次 run 开头一次性塞进 system prompt（见 `core/runner.py` 的 `load_context_file`）。
- **压缩摘要**：上下文超阈值后由 `Compactor` 生成的 `summary_<ts>.md`。

两者都是"全量或人工维护"的思路：要么整份注入，要么靠人工写 context。**缺的是"按需检索"**——当知识库很大、或分布在多个文件里时，agent 应该能带着问题主动查一次，而不是把整本手册塞进上下文。

RAG 模块补齐这一环，目标：

1. 提供一个 `rag_search` 工具，agent 可主动调用。
2. 索引本地知识库目录（递归 `.md` / `.txt`），检索返回**最相关的若干文本块 + 来源路径**。
3. 零外部依赖（不引入 embedding 模型、向量数据库、分词库），纯标准库实现，保证项目"可离线、可移植"的定位。
4. 通过 `[rag]` 配置段与 `KAMA_RAG_*` 环境变量可控启用，默认关闭。

---

## 1. 方案选型

| 维度 | 内置 TF-IDF 工具（本方案） | MCP 接入外部向量库 |
|------|---------------------------|-------------------|
| 外部依赖 | 无（标准库） | 需 MCP server + embedding 模型 / 向量库 |
| 检索质量 | 关键词级，中英文可用 | 语义级，更强 |
| 侵入性 | 新增 `core/rag/` 模块 + config/runner 各几行 | 零改动（走既有 `McpServerManager`） |
| 可移植性 | 高 | 依赖外部服务可用性 |

**决策**：首版内置 TF-IDF。理由——项目当前无任何 embedding/向量存储依赖，引入外部服务会破坏"本地单机可跑"的特性；TF-IDF 对文档检索的召回已足够用，且实现可完全自测。语义检索留作升级路径（§9）。

---

## 2. 架构

```
core/rag/
├── __init__.py     # 导出 RagChunk / RagStore / RagSearchTool
├── store.py        # RagStore：加载、分块、建 TF-IDF 索引、检索
└── tool.py         # RagSearchTool：BaseTool 子类，暴露 rag_search 工具
```

数据流：

```
knowledge_dir/**.md/.txt
        │  load_dir()  递归读取
        ▼
   切块 (_split_chunks)  →  RagChunk{id, source, text}
        │
        ▼
  分词 (_tokenize) + 建索引 (_build_index)  →  doc_vectors + idf
        │
        ▼
  rag_search(query)  →  TF-IDF 向量化 query → 余弦相似度 → top_k 个 RagChunk
```

### 2.1 RagStore（`store.py`）

- `load_dir(directory, exts=(".md", ".txt")) -> int`：递归收集文件、切块、建索引，返回 chunk 数。
- `search(query, top_k=3) -> list[RagChunk]`：检索并返回按相似度降序的 chunk。
- `_build_index()`：统计词频 df、算 IDF、生成每个 chunk 的 TF-IDF 稀疏向量（`dict[str, float]`）。
- `_cosine(a, b)`：稀疏向量余弦相似度。

关键数据结构：

```python
@dataclass
class RagChunk:
    id: str       # 形如 "path::chunk_index"，唯一标识
    source: str   # 来源文件路径
    text: str     # 文本块内容
```

### 2.2 RagSearchTool（`tool.py`）

遵循项目 `BaseTool` 契约：`name="rag_search"`、`description`、`input_schema`（手写 dict，供 LLM 用）、`params_model=RagSearchParams`（运行时校验用）、`retryable_errors`（只读工具，`runtime_error` 可安全重试）、`async invoke()` 返回 `ToolResult`。

```python
class RagSearchParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    query: str
    top_k: int = 3
```

`invoke` 将检索结果拼成带来源的文本返回：

```
[1] /path/to/notes.md
<chunk 文本>

[2] /path/to/other.md
<chunk 文本>
```

无匹配时返回 `"No matching chunks found in the knowledge base."`，不报错，让 agent 自然判断"知识库无相关内容"。

---

## 3. 检索原理：TF-IDF + 余弦相似度

**零依赖的核心取舍**——不用 embedding 模型，用统计关键词权重衡量相关性。

- **TF（词频）**：`tf(t, d) = d 中 t 出现次数 / d 的总 token 数`（按 chunk 长度归一化，避免长 chunk 天然占优）。
- **IDF（逆文档频率）**：`idf(t) = log((N+1)/(df+1)) + 1`（平滑版，df 为包含 t 的 chunk 数）。高频常见词权重被压低，稀有词权重被抬高。
- **向量**：chunk 的 TF-IDF 向量 = 每个 token 的 `tf × idf`，用 `dict[str, float]` 稀疏存储。
- **打分**：query 向量化后与每个 chunk 向量算**余弦相似度**，降序取 top_k。

```python
# 平滑 IDF：idf = log((N+1)/(df+1)) + 1
self._idf = {
    tok: math.log((n + 1) / (c + 1)) + 1.0
    for tok, c in df.items()
}
```

**优点**：可解释、可自测、冷启动无网络依赖。**缺点**：纯关键词匹配，无法理解"语义同义但用词不同"的查询（如查"怎么部署"匹配不到只写了"发布流程"的文档）——这是 §9 升级到 embedding 的动机。

---

## 4. 分块与分词

### 4.1 分块（`split_semantic`）

按语义边界切片，避免在句子或段落中间切断语义。流程分三步：

1. **结构块**：逐行扫描，按标题（`#`）、围栏代码块、普通段落切分；代码块整体保留，不参与句子切分。
2. **句切分**：超长段落按句末标点（`。！？. ! ?`）切句；标题、代码块与未超长段落直接作为一个单元。
3. **贪心合并（带 overlap）**：相邻单元贪心合并，直到总长不超过 `chunk_size`；开启 `chunk_overlap` 时，每块携带上一块**尾部 `chunk_overlap` 个字符**作为上下文前缀（计入 `chunk_size` 预算），保证标题与后续段落落在同一块，同时让相邻块在边界处保留重叠上下文、避免语义被切断。

默认 `chunk_size=800`、`chunk_overlap=100`，均可在配置里调。overlap 同时作用于硬切回退（`_split_chunks`，超长代码块/标题/单句）与语义合并（`_merge_units`）两条路径。

### 4.2 分词（`_tokenize`）

不引入 jieba 等分词库，用两条正则覆盖中英文：

- **英文/数字**：`[a-z0-9]+`，按单词切分。
- **中文**：`[一-鿿]` 抽取单个汉字，相邻两字组成 **bigram**（如"机器"、"器学"、"学习"），在无词典的前提下兼顾单字与常见词。

```python
def _tokenize(text: str) -> list[str]:
    lowered = text.lower()
    tokens = _WORD_RE.findall(lowered)
    han = _HAN_RE.findall(lowered)
    for i in range(len(han) - 1):
        tokens.append(han[i] + han[i + 1])
    return tokens
```

中文字符 bigram 是无词典分词的最小可用近似：能匹配两个连续字，覆盖绝大多数双字词。

---

## 5. 配置

新增 `RagConfig`，并入 `KamaConfig.rag`，沿用项目五层优先级（默认 → 全局 TOML → 项目本地 TOML → `.env` → 环境变量）。

```python
@dataclass
class RagConfig:
    enabled: bool = False                       # 是否注册 rag_search 工具
    knowledge_dir: str = "~/.kama/knowledge"    # 知识库根目录（递归 .md/.txt）
    top_k: int = 3                              # 每次检索返回 chunk 数
    chunk_size: int = 800                       # 分块字符数
    chunk_overlap: int = 100                    # 相邻块重叠字符数
```

**TOML**（`~/.kama/config.toml` 或 `.kama/config.toml`）：

```toml
[rag]
enabled = true
knowledge_dir = "~/.kama/knowledge"
top_k = 3
chunk_size = 800
chunk_overlap = 100
```

**环境变量**：`KAMA_RAG_ENABLED`、`KAMA_RAG_KNOWLEDGE_DIR`、`KAMA_RAG_TOP_K`、`KAMA_RAG_CHUNK_SIZE`、`KAMA_RAG_CHUNK_OVERLAP`。布尔按 `0/false/no` 判定为假，其余为真；数值非法时 `SystemExit`，与既有 `_apply_env` 一致。

`_apply_toml` 中新增 `[rag]` 段，未知 key、类型错误均硬退出，与其他段保持同一套校验风格。

### 5.1 rerank 精排阶段（可选，默认关闭）

检索默认是**单阶段**：`RagStore.search` → `index.search(query, top_k)` 直接返回 top_k（`HybridIndex` 内部用 RRF 做排名融合，仍是"召回"而非"精排"）。可选地在召回后加一个 **rerank 阶段**，用第二个模型对候选 chunk 按 `query+chunk` 相关性精排，在不调大 `top_k` 的前提下提升返回块的相关性密度。

**管线**：单阶段召回 `rerank_depth` 个候选 → `ZhipuReranker` 精排 → 取 `top_k` 返回。

- 实现：`core/rag/rerank.py` 的 `Reranker`（抽象基类）+ `ZhipuReranker`（智谱 `/rerank` 接口）。
- 复用 embedding 的 `api_key` 与 `base_url`；模型 `rerank_model` 默认 `rerank`（智谱官方公共重排序模型代码）。
- 候选集大小 `rerank_depth`（默认 50），受智谱接口 `documents ≤ 64` 上限约束，超出按 64 截断。
- 对任意 retriever（bm25/embedding/hybrid）生效，最常用在 hybrid 上（先保证召回，再精排）。
- 未配置 `api_key` 时自动降级为单阶段（reranker 为 None）。

配置键：`rerank_enabled`（bool，默认 `false`）、`rerank_model`（str，默认 `rerank`）、`rerank_depth`（int>0，默认 `50`）。环境变量：`KAMA_RAG_RERANK_ENABLED`、`KAMA_RAG_RERANK_MODEL`、`KAMA_RAG_RERANK_DEPTH`。

```toml
[rag]
rerank_enabled = true
rerank_model = "rerank"
rerank_depth = 50
```

### 5.2 向量数据库后端（Qdrant，可选）

默认检索后端把稠密向量存在**内存 + JSON 缓存**（`ZhipuEmbeddingIndex` 的 `.rag_index_cache.json`），检索是线性余弦遍历。当 chunk 规模变大、或需要多进程/服务共享索引时，可切到**真向量库**做 ANN 检索。

**当前实现：Qdrant**（`core/rag/vector_db.py` 的 `QdrantIndex`）。选择 Qdrant 的理由——它同时支持两种形态，用一行配置即可在「本地单机」与「服务/分布式」间切换，避免「Chroma 原型 → Qdrant 生产」的二次迁移：

- **local mode**：`QdrantClient(path=...)`，嵌入式、本地目录持久化、零部署，贴合项目「可离线、可移植」定位。
- **server mode**：`QdrantClient(url=...)`，对接 Docker/分布式（分片 + 副本），未来团队或语料规模扩大时无需改检索代码。

**职责边界**：Qdrant **只接管「存储 + 检索」**，embedding 仍由智谱 `embedding-3` 生成（复用 `ZhipuEmbeddingIndex` 的 `_embed`）。所以仍需配置 `api_key`；未配置 key 时仍回退本地 BM25。

**接口契合**：`QdrantIndex` 是又一个 `RagIndex` 实现，`RagStore`/`rag_search` 工具/`HybridIndex`/增量更新全都不改。为此 `RagIndex.build` 签名拓宽为 `(texts, ids, fingerprint, metas)`、`RagIndex.search` 拓宽为 `(query, top_k, allowed, filter)`——`ids` 是 `RagChunk.id`（`"{doc_id}::{i}"`）作稳定主键，`metas` 是各 chunk 的可过滤字段，`filter` 是检索时的键值过滤条件；内存后端（`Bm25Index`/`ZhipuEmbeddingIndex`）对新增参数直接 `del`，行为不变。

**三个关键细节**：

1. **稳定 id → UUID**：Qdrant 只接受 UUID/uint 作 point id，`"{doc_id}::{i}"` 不能直接用。`_point_id()` 用 `uuid5(NAMESPACE_URL, stable_id)` 把稳定 id 确定性压成 UUID 作 point id，原始 `stable_id` 存进 payload——幂等且保住稳定身份。
2. **增量靠 content_hash**：`build` 先 `scroll` 拉回 `{point_id: payload}`，只对「内容变化」的 chunk 调 embedding 再 `upsert`（未变 chunk 零 API 调用），并 `delete` 掉已不在语料中的陈旧点——延续现有按内容 hash 省钱的机制。同步的 Qdrant 调用经 `asyncio.to_thread` 包住，避免阻塞 daemon 事件循环。
3. **元数据写 payload + 服务端过滤下推**：每个点 payload 额外存 `meta`（chunk 的 `metadata`，值已类型化）。检索时 `filter` 转成 Qdrant `FieldCondition`（单值 `MatchValue`、列表 `MatchAny`、范围字典 `Range`，键前缀 `meta.`），经 `query_points(query_filter=...)` 下推——在服务端「先剪枝、再算相似度」，而非召回后内存过滤。内容未变仅 meta 变化时，只 `set_payload` 更新 `meta` 字段，不重算向量。

**词法路边界（有意不并入 Qdrant）**：混合检索里，`QdrantIndex` 只接管 **dense 语义路** 的 ANN 存储与检索，**词法一路保持内存 `Bm25Index`（倒排表 TAAT）**，`HybridIndex` 在内存里用 RRF 融合两路。评估过「Qdrant 原生稀疏向量 + 服务端 `FusionQuery` RRF」方案后放弃——它要求把 token 哈希成 uint32 稀疏维度并重写建库/查询路径，收益与现有倒排表等价，却引入非默认的稀疏编码。故词法路固定用 `Bm25Index` + 默认 `hash_text`（sha256 hexdigest），不做稀疏向量改造。

配置键：`vector_db`（str，默认 `""` 走 JSON 缓存；`"qdrant"` 启用）、`qdrant_url`（str，默认 `""` 走 local mode）、`qdrant_path`（str，默认 `~/.kama/qdrant`）。环境变量：`KAMA_RAG_VECTOR_DB`、`KAMA_RAG_QDRANT_URL`、`KAMA_RAG_QDRANT_PATH`。

```toml
[rag]
api_key = "..."               # 仍需智谱 key：embedding 由智谱生成
vector_db = "qdrant"          # 留空则走原来的 JSON 缓存
qdrant_path = "~/.kama/qdrant"   # local mode；上服务则改 qdrant_url = "http://host:6333"
```

### 5.3 可靠触发 RAG：三层干预机制

知识库规模扩大、主题增多后，单靠 LLM 自主判断「要不要检索」不可靠。为此从弱到强叠加三层干预，让触发从「模型自觉」升级为「硬性约束」：

**层 1 —— System Prompt 强制指令（最直接）**

`core/runner.py` 的 `_RAG_INSTRUCTION` 常量，在 `rag.enabled` 时注入系统提示：

> 在回答任何与本项目或领域知识相关的问题之前，必须优先调用 rag_search 检索本地知识库，并以检索结果为依据进行作答，严禁仅凭模型自身参数记忆直接回答。

注入点：`ExecutionContext` 新增 `rag_instruction` / `rag_manifest` 字段，`system_prompt()` 在 `## Knowledge Base Retrieval` 标题下拼接指令与清单。这样检索触发不再依赖模型的自主判断。

**层 2 —— 动态注入知识库清单（Manifest，应对多主题与变更）**

仅靠指令不够——Agent 无法预判知识库覆盖边界，可能「该检索没检索」或「检索了却查无此主题」。做法：

- `core/rag/chunk.py` 新增 `extract_topics()`：提取各级标题（去 `#` 与空白）；无标题时退回首行。
- `RagStore.load_dir` 遍历文件时同步提取主题，聚合为全局清单 `_topics`（去重保序），随 `manifest()` 返回，如 `知识库当前覆盖主题：部署手册、配置优先级、命名规范……`。
- 清单通过两条路径注入：`RagSearchTool.__init__` 拼进工具 `description`（让 Agent 看到工具就知道能查到什么）；同时 `runner` 把 `rag_store.manifest()` 注入系统提示。

优势：知识库内容变更时清单自动更新，Agent 能准确评估「是否应检索」与「能否检索到」。复用 `load_dir` 既有遍历逻辑，成本低。

**层 3 —— 结构化元数据过滤与多路检索（面向规模化）**

每个 chunk 可附带结构化元数据（来源、更新时间、部门等），检索时可组合语义相似度 + 业务条件做「先按业务路由、再精准检索」。

- **元数据来源**：`RagChunk` 新增 `metadata: dict[str, MetaValue]`（`MetaValue = str | int | float | bool | list[str]`）。`load_dir` 自动填充 `source`（相对路径）与 `updated_at`（mtime 的 `YYYY-MM`），并解析 Markdown frontmatter（`---` 之间的 `key: value`）取显式字段如 `department` / `category`，值按标量类型推断（`true`→bool、`10`→int、`0.5`→float）。
- **过滤语法（平铺键值）**：`rag_search` 的 `filter` 参数，键为字段名、值为：
  - 字符串/标量 → 等值匹配；
  - 字符串列表 → 任一匹配（`MatchAny`）；
  - 范围字典 `{"gte"/"lte"/"gt"/"lt": 边界}` → 范围过滤（数值优先按数值、退化字符串字典序），如 `{"updated_at": {"gte": "2026-03"}}`。
  平铺 JSON 比嵌套布尔表达式对 LLM 更友好、更不易生成错。
- **后端执行**：`RagStore.search(query, top_k, filter=...)` 把 filter 翻译成命中 chunk 的下标白名单（`_match`），`RagIndex.search(query, top_k, allowed)` 只在白名单内打分——BM25/embedding/hybrid/Qdrant 四后端一致。内存后端的 `_match` 用**字段值倒排索引**（`_build_inverted`）求各字段命中下标交集，避免全量线性扫描。

**关键：如何让 LLM 自己懂何时用过滤条件** —— 靠「词汇表 + 决策规则 + 示例」三注入，而非更聪明的 prompt：

1. **词汇表**：`RagStore.metadata_manifest()` 枚举每个可过滤字段及其去重后的全部取值，拼进工具 `description`。LLM 看到 `department: 研发、财务` 就不会幻觉出不存在的值。
2. **决策规则**：工具 `description` 写明启发式——「问题里出现某个字段取值时才带对应 filter，否则留空」。
3. **示例（one-shot）**：`description` 附带两个范例，帮 LLM 把规则落到具体参数上。

```python
# 示例：只检索「研发」部门的 chunk
result = await store.search("运行步骤", filter={"department": "研发"})

# 范围过滤：只检索 2026-03 之后更新的 chunk
result = await store.search("运行步骤", filter={"updated_at": {"gte": "2026-03"}})

# 词汇表注入后，工具 description 形如：
#   可过滤字段及其取值：
#   - department: 研发、财务
#   - source: rd.md、fin.md
#   - updated_at: 2026-09
```

**后端执行差异**：内存后端把 `filter` 翻译成下标白名单 `allowed`（经倒排索引加速），只在白名单内打分；`QdrantIndex` 额外把 `filter` 下推为服务端 `FieldCondition`/`Range`（见 §5.2 细节 3），先剪枝再算相似度——大规模语料下无需全量召回。二者对外行为一致。

**过滤正确性评测**：评测数据集（`RagEvalCase`）新增可选 `filter` 字段，`eval-rag --filter-check` 会额外计算「过滤合规率」（top-K 中命中允许集合的文档占比），把「过滤是否真的收窄候选集」从猜测变成可回归指标。

> 说明：`topic` 不再作为独立过滤参数——它从标题派生、词表不可控、精确匹配命中率低，作为过滤键价值有限；其价值在于 `manifest()` 展示（层 2），故保留为主题清单，不再参与过滤。

三层不互斥、可叠加：层 1 保证「会去检索」，层 2 保证「知道查什么」，层 3 保证「查得准、可规模化」。

### 5.4 查询优化与查询拓展（query 改写 + 多查询融合）

召回质量的一个常见瓶颈在**查询侧**：用户口语化提问（"这项目配置咋整的"）与知识库书面表达（"五层配置优先级"）用词不一致，单条 query 检索召回不足。本节在 `RagStore.search` 与检索后端之间插一层**查询处理器**，把原始 query 变成 1..N 条子查询后做 RRF 融合，分两条腿：

**腿 1 —— 确定性查询优化（零成本、始终开启）**

`optimize_query()` 只做空白规范化：`" ".join(query.split())`，压缩首尾与内部连续空白为单个空格。不依赖任何模型、不产生额外调用。

**腿 2 —— LLM 查询改写 + 拓展（可选、配置门控）**

`LlmQueryProcessor` 复用项目 `LLMProvider` 协议（`core/llm/base.py` 的 `chat` 一个方法），一次调用让模型返回 JSON 字符串数组：第 1 条是「书面化改写」，其后各条是「不同角度/同义表达/子问题」的拓展查询。原始查询始终保留在首位，其后追加拓展结果，去重并按 `1 + expand_n` 限幅。

- **失败安全回退**：LLM 调用异常、或输出无法解析为查询列表时，退化为仅原始查询，绝不阻断检索。
- **输出解析**：`parse_query_list()` 优先按 JSON 数组解析，退化逐行（剥离 markdown 围栏/项目符号/编号）。

**多查询 RRF 融合**：`rrf_fuse()` 从 `HybridIndex` 抽出为共享纯函数（`core/rag/index.py`），`RagStore.search` 对每条子查询召回 `query_depth` 个候选后按 RRF 融合取 top_k——让「多条查询都命中的 chunk」胜出。单查询时直接检索，零额外开销。

```python
# 配置了查询处理器时，改写+拓展为多条子查询；否则单条原始查询
if self._query_processor is not None:
    queries = await self._query_processor.transform(base)
else:
    queries = [base]
```

配置键：`query_enhance_enabled`（bool，默认 `false`）、`query_expand_n`（int>0，默认 `3`）、`query_depth`（int>0，默认 `10`）。环境变量：`KAMA_RAG_QUERY_ENHANCE`、`KAMA_RAG_QUERY_EXPAND_N`、`KAMA_RAG_QUERY_DEPTH`。

```toml
[rag]
query_enhance_enabled = true   # 按量计费，默认关闭
query_expand_n = 3             # 拓展子查询条数
query_depth = 10               # 每条子查询召回候选数
```

**A/B 评测**：`eval-rag --query-enhance` 会在检索层对同一 backend 追加 `+query` 变体，对比 baseline 与查询增强后的 Hit@K / MRR / Recall@K，把「改写+拓展是否真的提召回」从猜测变成可回归指标。

---

## 6. 接入点

### 6.1 runner 注册（`core/runner.py`）

`_build_registry` 里，紧跟 `NoteSaveTool` 之后：

```python
if self._config.rag.enabled and _ok("rag_search"):
    registry.register(RagSearchTool(self._get_rag_store(), self._config.rag.top_k))
```

- `_ok("rag_search")` 保证受 **skill 的 `tool_whitelist`** 过滤（与其它工具一致，白名单为空 == 不受限）。
- `_get_rag_store()` 惰性构建并**跨 run 复用** `RagStore`，避免每次 run 重读知识库重建索引：

```python
# 惰性构建并缓存 RagStore；首次调用时加载知识库目录并建索引
def _get_rag_store(self) -> RagStore:
    store = self._rag_store
    if store is None:
        store = RagStore(chunk_size=..., chunk_overlap=...)
        store.load_dir(Path(self._config.rag.knowledge_dir).expanduser())
        self._rag_store = store
    return store
```

> 注意：索引在 runner 生命周期内缓存，知识库文件变更需重启 daemon 才生效；如需热更新可后续加"文件 mtime 校验重建"（§9）。

### 6.2 子 agent（`subagent/tool.py`）

`rag_search` 默认**只注册给根 agent**（走 `_build_registry`）。子 agent 的 `_build_child_registry` 未加该工具——首版定位是"根 agent 的检索入口"。若要让子 agent 也能查知识库，可在 `_all_tools` 列表追加 `RagSearchTool` 并受 profile 的 `allowed_tools` 过滤（§9 可选增强）。

---

## 7. 用法示例

启用后，agent 面对需要项目背景知识的问题时会自发调用工具：

```
User: 这个项目的配置优先级是怎样的？
Agent: [调用 rag_search(query="配置优先级 config 优先级")]

ToolResult:
[1] /home/user/.kama/knowledge/kama_config.md
五层优先级：built-in defaults → 全局 ~/.kama/config.toml → 项目本地 .kama/config.toml → .env → env vars。
未知 key 会导致 SystemExit。
...

Agent: 根据知识库，配置优先级是……（引用检索结果作答）
```

无匹配时：

```
ToolResult: No matching chunks found in the knowledge base.
Agent: （据实说明知识库中没有相关内容，而非编造）
```

---

## 8. MCP 替代方案（升级路径）

若需要**语义检索**（embedding + 向量库），不改任何代码即可走既有 MCP 通道——KamaClaude 的 `McpServerManager` + `McpTool` 已完整支持 stdio/tcp 两类 transport。做法：

1. 准备一个暴露 `search` / `query` 工具的向量检索 MCP server（如基于 `chromadb`、`qdrant` 或 OpenAI/本地 embedding 的封装）。
2. 在 `config.toml` 的 `[mcp]` 段注册它：

   ```toml
   [[mcp.servers]]
   name = "rag"
   transport = "stdio"
   command = "python"
   args = ["-m", "my_rag_mcp"]
   ```

3. 重启 daemon，MCP 工具会以 `rag__search` 的命名自动进入 registry，agent 即可调用。

这与内置 `rag_search` 可并存：内置工具负责零依赖的快速召回，MCP 负责高质量语义检索，由 agent 自行决定用哪个。

> 注：§5.2 的 Qdrant 后端是「内置」路线——直接在 `RagIndex` 层接入向量库，embedding 仍走项目自带智谱客户端；而本节的 MCP 路线是把向量检索完全外包给外部 server。二者定位不同：Qdrant 后端用于「保留智谱 embedding + 本地持久化 + 增量缓存」的场景，MCP 用于「已有外部向量服务」的场景。

---

## 9. 局限与未来方向

**首版局限**：

1. **关键词匹配**：无语义泛化，同义异形查询召回不足。
2. **索引冷启动/静态**：索引建在内存，知识库变更需重启生效；大知识库建索引每次首查有一次性开销。
3. **无相关性阈值**：只要相似度 > 0 就返回，可能返回弱相关结果。
4. **子 agent 不可用**：`rag_search` 未下发给子 agent。
5. **单文件格式**：仅 `.md` / `.txt`，未支持 PDF/HTML。

**未来方向**：

- **embedding 升级**：`RagStore.search` 换成向量检索，`_tokenize` 换成 embedding 编码，接口不变，可平滑替换。
- **热更新**：记录文件 mtime，变更时局部重建索引。
- **相关性阈值 / 排序精调**：增加 `min_score`，或引入 BM25 替代纯 TF-IDF。
- **混合检索**：关键词 + 向量双路召回后重排（RRF）。
- **子 agent 共享**：把 `RagStore` 作为共享依赖注入 `_build_child_registry`。
- **分块策略增强**：按标题/段落结构化切分，替代固定窗口。
- **更多向量库后端**：`vector_db` 目前只支持 `"qdrant"`，可扩展 Chroma / Milvus 等，只需再实现一个 `RagIndex` 子类并在 `factory._dense` 加分支。

---

## 附：文件清单

| 文件 | 变更 |
|------|------|
| `src/kama_claude/core/rag/__init__.py` | 新增，导出模块 |
| `src/kama_claude/core/rag/chunk.py` | 新增，语义切片 + `extract_topics`（主题提取）+ `parse_frontmatter`（值类型推断，§5.3） |
| `src/kama_claude/core/rag/filter.py` | 新增，`MetaValue` 类型 + `matches`/`is_range`/`canonical` 过滤匹配原语 |
| `src/kama_claude/core/rag/query.py` | 新增，`optimize_query`/`parse_query_list` + `LlmQueryProcessor`（查询改写+拓展，§5.4） |
| `src/kama_claude/core/rag/index.py` | `rrf_fuse()` 抽为共享纯函数，`HybridIndex` 复用（§5.4） |
| `src/kama_claude/core/rag/store.py` | 新增，`RagStore` + 分块/索引 + `manifest()` + `metadata_manifest()` + 倒排索引过滤 + 多查询 RRF 融合（§5.4） |
| `src/kama_claude/core/rag/vector_db.py` | 新增，`QdrantIndex`（可选向量库后端，§5.2，含元数据 payload + 服务端过滤下推） |
| `src/kama_claude/core/rag/tool.py` | 新增，`RagSearchTool`（description 注入清单/词汇表/示例 + `filter` 参数） |
| `src/kama_claude/core/config.py` | 新增 `RagConfig` + `[rag]` TOML 段 + `KAMA_RAG_*` 环境变量 |
| `src/kama_claude/core/runner.py` | `_build_registry` 注册 `rag_search` + `_get_rag_store` 惰性缓存 + 注入 RAG 指令/清单 |
| `src/kama_claude/core/context.py` | 新增 `rag_instruction`/`rag_manifest` 字段并注入系统提示（§5.3） |
| `tests/unit/test_rag.py` | 新增，覆盖分词/分块/建索引/检索/工具/主题清单/过滤 |
| `tests/unit/test_rag_query.py` | 新增，覆盖查询优化/解析/RRF 融合/LLM 查询处理器回退（§5.4） |
| `docs/rag.md` | 本设计文档 |
