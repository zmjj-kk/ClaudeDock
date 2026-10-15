# KamaClaude 架构文档

本文档描述 KamaClaude 的整体架构、核心模块和数据流。

## 系统概览

KamaClaude 是一个**双进程架构**的本地 AI Agent 系统：

```
┌─────────────────────────────────────────────────────────────┐
│                     Client Layer                            │
├─────────────────┬───────────────────────────────────────────┤
│   kama (CLI)    │         kama-tui (TUI)                    │
│   快速测试/脚本  │         主要用户界面                        │
└────────┬────────┴──────────┬────────────────────────────────┘
         │                   │
         │   JSON-RPC 2.0    │
         │   (NDJSON/TCP)    │
         v                   v
┌─────────────────────────────────────────────────────────────┐
│              kama-core (Daemon)                             │
├─────────────────────────────────────────────────────────────┤
│  Session Manager │ LLM Provider │ Tool Registry            │
│  Permission Mgr  │ Memory Store │ RAG Index                │
│  Event Bus       │ Compactor    │ MCP Servers              │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        v
             ┌──────────────────────┐
             │  External Services   │
             ├──────────────────────┤
             │  • Anthropic API     │
             │  • Qdrant (Vector)   │
             │  • MCP Servers       │
             └──────────────────────┘
```

**核心设计原则：**
- 客户端无状态，所有状态在 daemon 中管理
- 客户端可随时断开重连，会话持续存在
- 单个 daemon 支持多个并发会话（TUI + CLI 同时使用）

---

## 核心模块

### 1. Protocol Layer (`src/kama_claude/core/bus/`)

所有 IPC 消息使用 **Pydantic v2 模型**，通过 `type` 字段实现**判别联合（Discriminated Union）**。

```python
# envelope.py
class JsonRpcRequest(BaseModel):
    jsonrpc: Literal["2.0"]
    method: str
    params: dict[str, Any]
    id: str | int

# commands.py
class PingCommand(BaseModel):
    type: Literal["ping"]
    client: str

Command = PingCommand | SessionCreateCommand | ...  # 联合类型

# events.py
Event = CoreStartedEvent | RunStartedEvent | ...
```

**协议文档自动生成：**
```bash
make docs  # 生成 WIRE_PROTOCOL.md
```

### 2. Transport Layer (`src/kama_claude/core/transport/`)

- **SocketServer**: 异步 TCP 服务器（`asyncio.start_server`）
- **协议**: 基于 NDJSON（每行一个 JSON-RPC 请求/响应）
- **端口探测**: 启动时检测端口占用，避免多实例冲突

```python
# 启动流程
server = SocketServer(host="127.0.0.1", port=7437)
server.register("core.ping", ping_handler)
await server.start()  # 监听并处理请求
```

### 3. Session Manager (`src/kama_claude/core/session/`)

管理多个并发会话，每个会话维护：
- **消息历史** (`ExecutionContext`)
- **工具调用状态** (`tool_results`)
- **压缩状态** (`CompactionResult`)

```python
session = SessionManager.create(system_prompt="...")
session.add_user_message("Hello")
# ...
session.close()
```

### 4. LLM Provider (`src/kama_claude/core/llm/`)

- **AnthropicProvider**: 封装 Anthropic SDK
- **流式响应**: 支持 SSE 流式输出（通过 EventBus 推送 token 事件）
- **Prompt Caching**: 自动启用（系统提示词 + 工具定义）

```python
response = await provider.chat(
    messages=context.messages,
    tool_schemas=registry.tool_schemas(),
    system="You are a helpful assistant.",
)
```

### 5. Tool System (`src/kama_claude/core/tools/`)

**工具注册表** (`ToolRegistry`):
- 动态注册工具（内置 + MCP）
- 生成 JSON Schema 定义
- 参数验证（Pydantic）

**内置工具**:
- `bash`: 执行 shell 命令
- `read_file`, `write_file`: 文件操作
- `list_dir`: 目录列表
- `memory_save`, `memory_search`: 长期记忆
- `task_create`, `task_update`: 任务管理

**MCP 集成** (`src/kama_claude/core/mcp/`):
- 动态加载 MCP 服务器
- 工具透明代理（MCP tool → Kama tool）

### 6. Permission System (`src/kama_claude/core/permissions/`)

**四层权限评估**:
1. **Deny Patterns** (最高优先级)：正则匹配黑名单
2. **Outside CWD Heuristics**：访问工作区外文件强制询问
3. **Session Always Cache**：会话内记忆（重启丢失）
4. **Persistent Always**：跨会话持久化（存储在 `~/.kama/policy.json`）

```python
decision = permission_manager.evaluate(tool_name, params)
if decision.action == "ask":
    allowed, _ = await permission_manager.check_and_wait(...)
```

### 7. Memory System (`src/kama_claude/core/memory/`)

**双存储架构**:
- **SQLite** (`MemorySQLiteStore`): 结构化记忆（元数据查询）
- **Qdrant** (`MemoryVectorStore`): 向量检索（语义相似性）

**记忆生命周期**:
```
用户提示 → LLM 调用 memory_save
         → 存储到 SQLite + Qdrant
         → 下次查询时 memory_search 检索
         → 注入到上下文
```

### 8. RAG System (`src/kama_claude/core/rag/`)

**三层检索**:
1. **Qdrant 向量检索**：语义相似性
2. **BM25 倒排索引**：关键词匹配
3. **Reranker**：合并与重排（RRF 算法）

**触发机制**:
- 项目根目录存在 `docs/` 时自动启用
- LLM 调用 `rag_search` 工具检索
- 系统提示词强制要求优先检索

### 9. Context Compaction (`src/kama_claude/core/compact/`)

**压缩策略**:
- 触发阈值：上下文使用 ≥ 80%
- 保留策略：
  - 系统提示词：完整保留
  - 最近 N 轮对话：完整保留
  - 历史工具结果：截断或删除

```python
compactor = Compactor(strategy="adaptive")
result = await compactor.compact(context, budget=200_000)
```

### 10. Event Bus (`src/kama_claude/core/events/`)

**发布-订阅模式**:
- 所有系统事件通过 EventBus 分发
- 支持多订阅者（Trace Writer、IPC Broadcaster、TUI）

```python
bus.subscribe(lambda event: print(event))
await bus.publish(RunStartedEvent(run_id="..."))
```

---

## 数据流

### Agent Run 生命周期

```
1. 客户端发送 agent.run 命令
   ↓
2. SessionManager 创建/获取 session
   ↓
3. AgentLoop 启动 plan-act-observe 循环
   ├─ [Plan] LLM.chat(messages, tools)
   │   └─ 事件: TokenEvent, UsageEvent
   ├─ [Act] 执行 tool_calls
   │   ├─ 权限检查 (Permission Manager)
   │   └─ 工具调用 (Tool Registry)
   └─ [Observe] 添加 tool_result 到上下文
   ↓
4. 循环结束条件：
   - stop_reason == "end_turn"
   - 达到最大步数
   - 用户中断
   ↓
5. 返回 RunFinishedEvent
```

### 权限审批流程

```
1. 工具调用触发权限检查
   ↓
2. PermissionManager.evaluate()
   - 命中 session cache? → 直接返回
   - 命中 persistent cache? → 直接返回
   - 否则 → action="ask"
   ↓
3. 发送 PermissionRequestEvent 到客户端
   ↓
4. 客户端显示权限对话框
   ↓
5. 用户选择: allow / deny / always allow
   ↓
6. 客户端发送 PermissionRespondCommand
   ↓
7. PermissionManager 更新缓存并返回
```

---

## 配置系统

**五层优先级** (从高到低):
1. **环境变量** (`KAMA_*`)
2. **`.env` 文件**
3. **项目配置** (`.kama/config.toml`)
4. **全局配置** (`~/.kama/config.toml`)
5. **内置默认值**

```toml
# ~/.kama/config.toml
[server]
host = "127.0.0.1"
port = 7437

[logging]
level = "INFO"
format = "json"

[llm]
default_model = "claude-sonnet-4-6"
```

---

## 安全设计

### 1. 路径遍历防护
- 所有文件操作通过 `PathValidator` 验证
- 限制访问工作区外的敏感目录

### 2. 密钥脱敏
- 日志中自动脱敏 API key (`sk-ant-***`)
- 审计日志使用 `SecretManager.sanitize()`

### 3. 审计日志
- 所有权限决策记录到 `~/.kama/audit.log`
- 工具调用记录（JSON Lines 格式）

---

## 性能监控

### 关键指标

| 指标 | 说明 | 目标 |
|------|------|------|
| `llm.chat` | LLM 调用延迟 | P95 < 2s |
| `tool.exec` | 工具执行时间 | P95 < 500ms |
| `rag.search` | RAG 检索延迟 | P95 < 200ms |
| `memory.search` | 向量检索延迟 | P95 < 100ms |

### OpenTelemetry 集成

```bash
# 启用 OpenTelemetry 导出
export OTEL_ENABLED=true
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
uv run kama-core
```

---

## 扩展开发

### 添加新工具

```python
# src/kama_claude/core/tools/builtin/my_tool.py
from kama_claude.core.tools.base import Tool

class MyTool(Tool):
    name = "my_tool"
    description = "工具描述"

    # 工具参数模型（自动生成 JSON Schema）
    class Parameters(BaseModel):
        arg1: str
        arg2: int = 0

    async def execute(self, args: Parameters) -> str:
        return f"Result: {args.arg1}"

# 注册到 ToolRegistry
registry.register(MyTool())
```

### 添加新命令

```python
# 1. 定义命令模型
class MyCommand(BaseModel):
    type: Literal["my_command"]
    param: str

# 2. 添加到 Command 联合
Command = ... | MyCommand

# 3. 注册处理器
async def my_handler(params: dict) -> dict:
    cmd = MyCommand(**params)
    return {"result": cmd.param}

server.register("my.command", my_handler)
```

---

## 故障排查

### 常见问题

**端口占用**:
```bash
# 检查端口占用
lsof -i :7437
# 或
netstat -tulpn | grep 7437
```

**日志调试**:
```bash
# 启用详细日志
KAMA_LOG_LEVEL=DEBUG uv run kama-core
```

**清理状态**:
```bash
# 清理所有运行时数据
rm -rf runs/ ~/.kama/
```

---

## 相关文档

- [WIRE_PROTOCOL.md](../WIRE_PROTOCOL.md) - IPC 协议定义
- [CONTRIBUTING.md](../CONTRIBUTING.md) - 开发指南
- [RUNBOOK.md](../RUNBOOK.md) - 运维手册
