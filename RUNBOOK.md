# 运维手册（RUNBOOK）

## 日常操作

### 启动守护进程

```bash
uv run claudedock-core
```

默认监听 `127.0.0.1:7437`，按 `Ctrl+C` 优雅退出。

### 验证连通

```bash
uv run claudedock ping
# → pong server=0.0.1 uptime=12ms latency=2ms
```

### 停止守护进程

```bash
kill $(pgrep -f claudedock-core)
```

---

## 配置

优先级（低 → 高）：**内建默认值 → `~/.claudedock/config.toml` → `.env` → 系统环境变量**。

### `~/.claudedock/config.toml`

```toml
[core]
host = "127.0.0.1"
port = 7437

[logging]
level  = "INFO"
file   = "~/.claudedock/logs/core.log"
format = "text"    # "text" | "json"
```

### `.env`

从 `.env.example` 复制后修改，存放本机配置与密钥（不提交 git）：

```bash
cp .env.example .env
```

### 系统环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CLAUDEDOCK_CONFIG` | `~/.claudedock/config.toml` | 覆盖配置文件路径 |
| `CLAUDEDOCK_HOST` | `127.0.0.1` | TCP 监听地址 |
| `CLAUDEDOCK_PORT` | `7437` | TCP 监听端口 |
| `CLAUDEDOCK_LOG_LEVEL` | `INFO` | 日志级别（DEBUG / INFO / WARNING / ERROR） |
| `CLAUDEDOCK_LOG_FILE` | `~/.claudedock/logs/core.log` | 日志文件路径（留空则仅输出 stderr） |
| `CLAUDEDOCK_LOG_FORMAT` | `text` | 日志格式（`text` 或 `json`） |

---

## 开发

```bash
uv run ruff check src tests scripts   # lint
uv run mypy src                       # 类型检查
uv run pytest tests/ -v               # 全量测试
uv run pytest tests/unit/ -v         # 仅单元测试（无需启动 daemon）

make docs                             # 重新生成 WIRE_PROTOCOL.md
make verify-s0                        # 完整验证（lint + 类型 + 测试 + 协议同源检查）
```

---

## 日志

```bash
tail -f ~/.claudedock/logs/core.log
```

---

## 常见错误

| 报错 | 原因 | 处理 |
|------|------|------|
| `core already running at 127.0.0.1:7437` | 已有守护进程在运行 | `kill $(pgrep -f claudedock-core)` |
| `core not running` | 未启动守护进程 | `uv run claudedock-core` |
| `Address already in use` | 端口被其他进程占用 | `CLAUDEDOCK_PORT=8000 uv run claudedock-core` |
| `Config error: CLAUDEDOCK_PORT must be an integer` | `.env` 或环境变量中端口值非整数 | 检查 `CLAUDEDOCK_PORT` 的值 |

## 生产升级运维

2026-10-14 的认证、预算、容器与恢复流程见 [生产运维手册](docs/production/operations.md)。


## 并行工具执行（默认关闭）

`[tool_execution]` 配置控制同轮工具调度。`parallel_enabled=false` 为默认值；设置 `shadow_enabled=true` 可保留串行执行并记录理论并发规划。设置 `parallel_enabled=true` 后，目前只为经审核的内置 `read_file`、`list_dir` 开放并发，写入、Shell、MCP 和子 Agent 启动仍形成串行屏障。

`max_concurrency_per_run`（默认 4）控制单运行执行槽，`max_calls_per_batch`（默认 8）限制批次数量，`max_pending_approvals`（默认 8）限制共享权限管理器待审批数量；全局执行槽继续使用 `runtime.max_tool_requests`。审批等待不占执行槽。超限配置直接报错，不会静默扩大资源范围。

回滚时将 `parallel_enabled` 设为 false 并按现有流程重启守护进程。工具超时或取消后先检查 Journal 的未决状态，不要直接重复可能已完成的副作用操作。当前本地文件基准未显示提速；启用前请阅读 `docs/production/parallel-tool-execution-implementation.md`，按实际负载验证后再灰度。
