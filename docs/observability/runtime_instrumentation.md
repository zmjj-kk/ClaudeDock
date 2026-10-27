# ClaudeDock 运行时埋点设计（Runtime Instrumentation）

> 定位：APM 设计文档（`apm_design.md`）的**运行时实现篇**。核心思想——不靠离线推导，而是在执行链里**原生发射 Span**，让 Span 成为性能数据的权威来源；离线推导降级为"回填历史 `events.jsonl`"的兼容工具。
> 配套：`docs/observability/apm_design.md`（Span 模型与六类瓶颈）、`docs/evaluation/`（评测体系）。

---

## 0. 设计目标

1. **运行时原生发射 Span**，含父子关系、并行语义、维度属性，落到 `spans.jsonl`。
2. **零签名破坏**：用 `contextvars` 携带当前 tracer，叶子埋点无需改函数签名（不改 `invoke_tool`/`provider.chat`/各工具 `invoke` 的参数）。
3. **纠正两个既有的归因错误**：权限等待被算进工具耗时；重试各轮次无独立记录。
4. **补齐模型侧三段分解**（连接 / 首 Token / 生成），支撑"排队 vs 模型"的准确归因。

---

## 1. 基础设施

### 1.1 Span 模型

沿用 `apm_design.md` §2 的 `Span` 模型（`span_id/trace_id/parent_span_id/name/kind/start_ts/end_ts/duration_ms/status/parallel/attributes/events`）。新增文件 `src/claudedock/observability/span.py`。

### 1.2 SpanWriter（落盘）

镜像 `core/trace/writer.py` 的队列 + drain 模式，新增 `observability/writer.py`：

```python
class SpanWriter:
    def __init__(self, path: Path) -> None: ...          # 队列 + 后台 drain task
    async def start(self) -> None: ...                    # 建目录、启动 drain
    async def stop(self) -> None: ...                     # 排空队列后取消 drain
    def emit(self, span: Span) -> None: ...               # 非阻塞入队，drain 写 spans.jsonl 每行一个 Span
```

- 首版只落**已完成的 Span**（`start_ts/end_ts/duration_ms/status` 已填全）。崩溃中途丢失的 Span 可后续加"start 标记"解决，首版不做。
- 每行一个 `Span.model_dump_json()`，风格对齐 `events.jsonl` / `daemon.jsonl`。

### 1.3 SpanTracer（埋点入口，contextvars 携带）

新增 `observability/tracer.py`，核心是一个 `contextvars.ContextVar`：

```python
_current: ContextVar["SpanTracer | None"] = ContextVar("claudedock_span_tracer", default=None)

def set_tracer(t: SpanTracer | None) -> Token: ...        # 在 run 边界 set，返回 reset token
def get_tracer() -> SpanTracer | None: ...                # 叶子埋点取用；无 tracer 时静默 no-op

class SpanTracer:
    def __init__(self, writer: SpanWriter, trace_id: str) -> None: ...
    @asynccontextmanager
    async def span(self, *, name, kind, parent, **attrs) -> AsyncIterator[Span]: ...
```

- **关键机制**：`asyncio.create_task` 会复制创建时的 context，因此**后台 subagent 自动继承 tracer**，无需显式传递。
- **测试友好**：未 `set_tracer` 时 `get_tracer()` 返回 `None`，所有埋点静默跳过，既有 266 个单测零影响。

### 1.4 接入点

在 `AgentRunner.run_and_capture`（`core/runner.py`）里，与 `EventWriter` 并列创建并启动 `SpanWriter`，`set_tracer(SpanTracer(writer, trace_id=run_id))`，run 结束 `reset`。Span 与 Event 共享 `run_id` 作为关联键（§5）。

---

## 2. 埋点总览（按执行链，含优先级）

| # | 埋点 | 位置 | kind | 关键属性 | 优先级 |
|---|---|---|---|---|---|
| 1 | Run Span | `runner.py` `run_and_capture` | `run` | `task_type`(新增), `version`(新增) | P1 |
| 2 | Step Span | `loop.py` `run` | `step` | `step`, `stop_reason` | P1 |
| 3 | LLM Span + 三段分解 | `llm/provider.py` `chat` | `llm` | `model`, `ttft_ms`, `generation_ms`, `input/output_tokens`, `cache_read` | P1 |
| 4 | Tool Span | `tools/invocation.py` `invoke_tool` | `tool` | `tool_name`, `error_class`, `output_bytes` | P1 |
| 5 | 权限等待子 Span | `invocation.py`（权限检查前后） | `permission_wait` | `wait_ms`, `decision` | **P0（修复归因）** |
| 6 | 重试尝试子 Span | `invocation.py` 重试循环 | `retry` | `attempt`, `retry_of`, `backoff_ms`, `error_class` | P1 |
| 7 | Subagent Span | `subagent/tool.py` | `subagent` | `subagent_type`, `run_in_background`, `depth`, `parallel` | P1 |
| 8 | 工具体量 / 冷启动 | `bash.py`, `mcp/tool.py` | （tool Span 属性） | `output_bytes`, `cold_start_ms` | P2 |
| 9 | 资源采集 | 新增 `observability/resources.py` | `resource`(定时样本) | `cpu_pct`, `rss_mb`, `threads` | P3 |
| 10 | 模型 connect_ms | `provider.py` + 自定义 httpx client | `llm` 属性 | `connect_ms` | P3 |
| 11 | 下游/MCP 三段延迟 | `mcp/client.py` | `tool` 子 Span | `connect_ms`, `request_ms`, `response_ms` | P3 |

> 优先级：P0 是"纠正既有错误"（权限等待），P1 是"零成本可立即产出瀑布图/关键路径/TTFT 分布"的主体，P2/P3 是"更细的下钻与资源归因"。

---

## 3. 各埋点详细设计

### 3.1 Run Span（#1）— 新增两个业务维度

在 `run_and_capture` 的 `RunStartedEvent` 发布处，同时开启 run Span。**须新增两个维度字段**（否则 Step 1 的"按业务切分"做不完整）：

- `task_type`：对 `goal` 做轻量分类（文件操作 / 代码生成 / 多步编排 / 检索问答 …）。首版用规则/关键词打标，后续可换小模型分类。
- `version`：发布版本，从构建产物或 `CLAUDEDOCK_VERSION` 环境变量注入。

### 3.2 LLM Span + 三段分解（#3）★最关键的埋点

位置：`llm/provider.py` 的 `chat()`，在 `stream()` 前后打点：

```python
t0 = time.monotonic()
first_ts: float | None = None
async with self._client.messages.stream(**kwargs) as stream:
    async for text in stream.text_stream:
        if first_ts is None:
            first_ts = time.monotonic()          # 首 Token 到达
        ...
t_last = time.monotonic()

ttft_ms        = (first_ts - t0) * 1000          # 时间到首 Token（含连接+排队+预填充）
generation_ms  = (t_last - first_ts) * 1000      # 首 Token 到末 Token（生成速度）
```

- **ttft_ms / generation_ms 用 `time.monotonic()` 即可精确测量**，这是六类瓶颈第二类"模型调用"拆分的核心。
- **解读**（对应你的原话）：ttft 上涨但 generation 正常 → 服务排队 / 上下文变长 / 预填充变慢；首 Token 快但总时延长 → 输出变长 / 生成速率下降 / 触发更多步骤。
- **注意**：现有 `provider.py:104` 只在 `attempt == 1` 发 `LlmTokenEvent`；Span 的 ttft/generation 也应只对**首次成功的 stream 尝试**计一次，重试另起 `retry` 子 Span（§3.4）。

### 3.3 权限等待子 Span（#5）★修复既有归因错误

位置：`invocation.py` 的 `check_and_wait` 调用前后。当前 `t0` 在权限检查之前打点，导致等待被算进 `ToolCallFinishedEvent.elapsed_ms`。改为：

```python
t0 = time.monotonic()                       # 工具 Span 起点
...
if permission_manager is not None:
    t_perm = time.monotonic()
    allowed, decision = await permission_manager.check_and_wait(...)   # 可能阻塞至 60s
    async with tracer.span(name="permission_wait", kind="permission_wait",
                           parent=tool_span_id,
                           wait_ms=(time.monotonic()-t_perm)*1000, decision=decision):
        pass                                  # 等待时长独立成子 Span
```

- 效果：工具 Span 的 `duration_ms` 只含真实执行，等待时长单独可见。这是"排队"类瓶颈在 ClaudeDock 里的权威度量。

### 3.4 重试尝试子 Span（#6）

`invoke_tool` 的重试循环（`for attempt in range(1, _MAX_RETRIES+2)`）里，每次 `tool.invoke` 包一层 `retry` 子 Span：

- 属性：`attempt`（1/2/3）、`retry_of`（指向逻辑调用的 tool Span）、`backoff_ms`（本次退避时长，从 `_RETRY_BASE_S * 2**(attempt-1)` 读出）、`error_class`。
- 关联关系：`retry` Span 是 `tool` Span 的子；多个 `retry` 兄弟之间用 `retry_of` 标记"同一次逻辑调用"。
- `provider.py` 的流重试（`_MAX_STREAM_RETRIES=3`）同理，每个 attempt 一个 `retry` 子 Span。

### 3.5 Subagent Span + 并行语义（#7）

位置：`subagent/tool.py` 的 `invoke`。后台模式（`run_in_background=true`）与前台模式分别发射：

- 前台：同步 `await child_loop.run()` → `subagent` Span，`parallel=false`。
- 后台：`asyncio.create_task(...)` → `subagent` Span `parallel=true`，`start_ts/end_ts` 跨父步骤后续执行，**时间区间与父后续 tool Span 重叠**，关键路径算法据此对并行孩子取 max。
- 属性：`subagent_type`（planner/executor/reviewer）、`depth`（嵌套层数）、`parent_run_id`。

### 3.6 工具体量 / 冷启动（#8）

- `bash.py`：`proc` 创建到 `communicate()` 返回的时间差 = `cold_start_ms`（子进程冷启动）；`len(stdout_bytes)` = `output_bytes`。
- `mcp/tool.py`：MCP server 首次连接建立耗时 = `cold_start_ms`。

### 3.7 资源采集（#9）— 独立后台任务

新增 `observability/resources.py`：一个 `asyncio` 周期任务（默认每 5s），采样 `psutil.cpu_percent()`、`Process.memory_info().rss`、线程数，写 `resource` 样本（带 `ts`，不强制绑 `run_id`，按时间与 Span 区间关联）。支撑 Step 4 的"应用内部计算 → CPU/内存/线程池"下钻。

### 3.8 模型 connect_ms（#10）

ttft/generation 用 SDK 高层 API 即可测；但"连接 / TLS / 供应商排队"要拆到 `connect_ms`，需给 `anthropic.AsyncAnthropic` 注入带 event hooks 的 `httpx.AsyncClient`（`request`/`connect`/`response` hook 打点）。标记为 P3（侵入 SDK 传输层，成本最高）。

---

## 4. 存储与消费

```
spans.jsonl（运行时产出，每行一个已完成的 Span）
   │
   ├─ metrics.py   → TTFT/总时延/P50/P95/P99/成功率/超时率，按 model·tool·task_type·version 聚合（Step 1）
   ├─ waterfall    → 瀑布图 + 关键路径（Step 2/3）
   └─ drilldown    → 下钻日志/错误/资源（Step 4）
```

---

## 5. 与既有事件/追踪的关系（去冗余）

| 流 | 角色 | 与 Span 关系 |
|---|---|---|
| `events.jsonl` | 领域事件（发生了什么，供 TUI 与回放） | 保持不动；Span 与 Event 共享 `run_id`/`step` 关联 |
| `daemon.jsonl` (Trace) | IPC / LLM I/O 追踪 | `TracingProvider` 的 `api_response.latency_ms` 与 llm Span 有重叠；**Span 成为延迟权威来源**，Trace 保留用于 IPC 层排查与向后兼容 |
| `spans.jsonl`（新增） | 性能结构（父子/并行/维度） | 权威延迟来源 |

> 迁移策略：llm Span 先落，`TracingProvider` 暂留（不破坏 `claudedock trace`），待指标/瀑布图消费方稳定后，再把 Trace 的延迟分析统一迁到 Span。

---

## 6. 开销与正确性注意

1. **开销**：`LlmTokenEvent` 目前逐 token 发事件（provider 已有），Span 只在首/末 token 打两个点，开销可忽略；资源采集是周期低频任务。
2. **时钟**：Span 一律用 `time.monotonic()` 测时长，`datetime.now(UTC).isoformat()` 记 `ts`（与 Event/Trace 同源，便于关联）。
3. **异步上下文**：`contextvars` 随 `asyncio.create_task` 传播 → 后台 subagent 自动继承 tracer；但**注意 `asyncio.to_thread` / 同步线程池不传播 context**，资源采集若走线程需显式传递。
4. **子代理隔离**：child run 用独立 `trace_id`（child_run_id），与父通过 `parent_run_id` 关联，不污染父 Span 树。
5. **异常安全**：Span 上下文管理器在 `except`/`finally` 里保证 `end_ts/status` 一定落盘，即使工具抛异常（`invoke_tool` 不抛，但 llm/provider 可能抛，须兜住）。

---

## 7. 落地顺序（运行时优先）

| 阶段 | 埋点 | 产出能力 |
|---|---|---|
| **R0** | #5 权限等待拆分（修复归因） | 立刻纠正"排队算成工具慢" |
| **R1** | #1 Run + #2 Step + #3 LLM(ttft/generation) + #4 Tool + #6 重试 + #7 Subagent | 瀑布图 + 关键路径 + TTFT/分位分布（Step 1~3 全通） |
| **R2** | #8 工具体量/冷启动 + #11 MCP 三段 | 工具/下游下钻（Step 4 部分） |
| **R3** | #9 资源采集 + #10 connect_ms | 应用内部计算 + 模型连接段下钻（Step 4 完整） |

> 与原 APM 文档 `§7` 的差异：这里把"权限等待拆分"提到 R0（因为它修复的是**既有错误**，不依赖 Span 消费方，可独立交付），其余埋点顺序不变。

---

## 8. 已确认的设计决策（2026-09-17 拍板）

1. **Span 独立落盘，不进 EventBus**：用独立 `SpanWriter` 写 `spans.jsonl`，保持事件流纯净、Span 与事件职责分离。
2. **`task_type` 首版规则打标**：对 `goal` 做关键词/规则分类（文件操作 / 代码生成 / 多步编排 / 检索问答），零 LLM 成本、`run.started` 无需等 LLM；后续可换小模型分类。
3. **`version` 构建注入**：从构建产物或 `CLAUDEDOCK_VERSION` 环境变量注入 root Span。

> 对 §1.2 的细化：SpanWriter 只管落盘、不订阅 EventBus；如需 TUI 实时渲染 Span，另加一个只读订阅器消费 `spans.jsonl`（不反向污染事件流）。
