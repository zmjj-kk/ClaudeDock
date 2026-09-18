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

### 4.1 分块（`_split_chunks`）

固定窗口滑动切块，相邻块带 `chunk_overlap` 重叠，避免语义在块边界被切断：

```python
step = max(1, size - overlap)
while start < len(text):
    chunks.append(text[start:start + size])
    start += step
```

默认 `chunk_size=800`、`chunk_overlap=100`，均可在配置里调。

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

---

## 附：文件清单

| 文件 | 变更 |
|------|------|
| `src/kama_claude/core/rag/__init__.py` | 新增，导出模块 |
| `src/kama_claude/core/rag/store.py` | 新增，`RagStore` + 分词/分块/TF-IDF |
| `src/kama_claude/core/rag/tool.py` | 新增，`RagSearchTool` |
| `src/kama_claude/core/config.py` | 新增 `RagConfig` + `[rag]` TOML 段 + `KAMA_RAG_*` 环境变量 |
| `src/kama_claude/core/runner.py` | `_build_registry` 注册 `rag_search` + `_get_rag_store` 惰性缓存 |
| `tests/unit/test_rag.py` | 新增，覆盖分词/分块/建索引/检索/工具 |
| `docs/rag.md` | 本设计文档 |
