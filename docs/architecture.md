# ClaudeDock 架构与规范

本文档描述 ClaudeDock 的核心设计思路、模块划分、代码目录规范与命名规范，是贡献代码前的必读材料。

## 核心设计思路

ClaudeDock 是一个**双进程**的本地 AI Agent 系统：`claudedock-core` 是常驻守护进程，`claudedock` 与 `claudedock-tui` 是连接它的客户端，三者通过 TCP 上的 JSON-RPC 2.0 NDJSON 通信。

```text
claudedock-core (daemon)
  └─ 监听 127.0.0.1:7437 (TCP)
       ↑ JSON-RPC 2.0 NDJSON
claudedock (CLI)   claudedock-tui (TUI, 主前端)
```

- **TUI 优先**：`claudedock-tui` 是主要前端，所有任务管理、可观测性、交互功能都应先为 TUI 设计并验证。`claudedock` CLI 只用于快速脚本化调试，不是产品面。
- **类型即契约**：所有 IPC 消息都是 pydantic v2 模型，用 `type` 字段做判别联合（discriminated union）。新增命令或事件，就是在 `bus/commands.py` / `bus/events.py` 里加模型类并扩展联合类型。
- **五层配置优先级**：内置默认值 → 全局 `~/.claudedock/config.toml` → 项目本地 `.claudedock/config.toml` → `.env` → 环境变量，后者覆盖前者。未知配置键会硬退出。
- **事件驱动可观测**：LLM token、工具调用、权限审批、run 起止等都以事件发布到 `EventBus`，TUI 与 trace 文件订阅同一事件流。
- **权限门控**：破坏性工具（如 bash、写文件）经 `PermissionManager` 审批，支持超时与自动允许/拒绝策略。

## 模块划分

源码位于 `src/claudedock/`，运行时逻辑集中在 `core/` 包。

| 目录 | 职责 |
|------|------|
| `core/bus/` | 协议层：`envelope.py`（JSON-RPC 信封）、`commands.py`、`events.py`（判别联合模型） |
| `core/transport/` | 传输层：`socket_server.py`（TCP 服务）、`socket_client.py`、`ipc_broadcaster.py` |
| `core/config.py` | 配置加载与校验（五层优先级） |
| `core/app.py` | 守护进程入口 `CoreApp.run()` |
| `core/runner.py` | `AgentRunner`：组装一次 run 的全部运行时依赖与工具注册表 |
| `core/loop.py` | `AgentLoop`：驱动 LLM 与工具调用的主循环 |
| `core/context.py` | `ExecutionContext`：记录单次 run 的状态与结果 |
| `core/llm/` | LLM 提供方：`base.py`（抽象）、`provider.py`（Anthropic 兼容实现）、`types.py` |
| `core/tools/` | 工具框架：`base.py`（`BaseTool`/`ToolResult`）、`registry.py`、`invocation.py`（重试）、`builtin/` |
| `core/task/` | 任务管理：`manager.py`、`model.py` |
| `core/session/` | 会话：`model.py`、`store.py`、`manager.py` |
| `core/memory/` | 上下文文件加载 `loader.py` |
| `core/permissions/` | 权限：`manager.py`、`policy.py`、`storage.py`、`errors.py` |
| `core/compact/` | 上下文压缩：`compactor.py`、`budget.py` |
| `core/subagent/` | 子代理：`tool.py`（SpawnAgentTool/AgentResultTool）、`registry.py` |
| `core/mcp/` | MCP 集成：`server.py`、`client.py`、`tool.py` |
| `core/skills/` | 技能加载：`loader.py`、`builtin/` |
| `core/agents/` | Agent 定义加载：`loader.py`、`builtin/` |
| `core/rag/` | 本地知识库检索：`chunk.py`（语义切片）、`index.py`（TF-IDF / embedding）、`store.py`、`tool.py` |
| `core/trace/` | 追踪：`provider.py`、`writer.py`、`record.py` |
| `core/events/` | 事件总线：`bus.py`、`writer.py` |
| `core/runs.py` | run id 生成与目录 |
| `core/logging_setup.py` | 日志初始化 |

## 代码目录规范

```text
src/claudedock/
  core/        # 守护进程与运行时（上面已列）
  cli/         # claudedock 命令行客户端（commands/ 下按子命令拆分）
  tui/         # claudedock-tui（textual 框架）
  eval/        # 评测相关
tests/
  unit/        # 纯单元测试（不联网、不启动守护进程）
  integration/ # 集成测试（fixture 会起真实守护进程子进程）
scripts/
  gen_protocol_doc.py  # 从 bus 模型重新生成 WIRE_PROTOCOL.md
docs/
  architecture.md  # 本文档
  *.md             # 其他项目文档
```

关键约定：

- 源码用 **src 布局**，包根是 `src/claudedock`。
- `WIRE_PROTOCOL.md` 由 `scripts/gen_protocol_doc.py` **生成**，改了 bus 模型后必须重新生成并提交。
- 规划性设计文档放在仓库外的 `../docs/`（本仓库的兄弟目录），不随代码提交。

## 命名规范

- **文件/模块**：`snake_case`（如 `socket_server.py`、`rag_search`）。
- **类名**：`PascalCase`（如 `RagStore`、`BaseTool`、`AgentRunner`）。
- **函数/方法**：`snake_case`；模块私有函数加前导下划线（如 `_tokenize`、`_split_chunks`）。
- **常量**：`UPPER_SNAKE_CASE`，模块私有加前导下划线（如 `_DEFAULT_PORT`）。
- **bus 消息模型**：以 `Command`/`Event` 结尾并纳入判别联合；错误码常量化在 `envelope.py`。

## 代码风格

所有函数必须在 `def` 上方有一行**中文注释**说明用途，不写多行 docstring：

```python
# 发送 JSON-RPC 响应并刷新写缓冲区
async def _send(self, writer: asyncio.StreamWriter, msg: BaseModel) -> None:
    ...
```

测试函数则需要**两行**中文注释：

```python
# 功能：验证 publish 后订阅者能收到事件对象
# 设计：用内联 handler 收集事件引用，断言 is 而非 ==，排除序列化中间步骤的干扰
async def test_publish_reaches_subscriber() -> None:
    ...
```

## 常用命令

```bash
uv sync                              # 安装/同步依赖
uv run ruff check src tests scripts  # lint
uv run mypy src                      # 类型检查
uv run pytest tests/unit -v          # 单元测试
uv run pytest tests/integration -v   # 集成测试
uv run claudedock-core                     # 前台运行守护进程
uv run claudedock ping                     # 发送 ping
```

## RAG 知识库

`rag_search` 工具从本地知识库目录（默认 `~/.claudedock/knowledge`）递归读取 `.md` 与 `.txt` 文档，先按语义边界切片（标题 → 段落 → 句子），再用智谱 `embedding-3` 生成向量做余弦检索；未配置 API key 时回退到零依赖的 TF-IDF。索引向量会落盘缓存，避免重复计费。相关配置见 `[rag]` 段，如 `chunk_size`、`chunk_overlap`、`embedding_dimensions` 等。
