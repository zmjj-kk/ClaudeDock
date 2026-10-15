# KamaClaude

<div align="center">

**本地 AI Agent 系统 · 双进程架构 · 多会话支持**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)

[快速开始](docs/getting-started/quickstart.md) • [架构文档](docs/architecture/overview.md) • [API 参考](docs/api/protocol.md) • [贡献指南](CONTRIBUTING.md)

</div>

---

## ✨ 特性

- 🔄 **双进程架构**: 守护进程（`kama-core`）+ 客户端（`kama-tui` / `kama`）
- 🖥️ **丰富的用户界面**: 功能完整的 TUI 和快速的 CLI
- 🧠 **长期记忆**: 跨会话持久化，支持向量检索和语义搜索
- 📚 **RAG 集成**: 自动索引项目文档，支持混合检索（向量 + BM25）
- 🔐 **权限管理**: 四层权限策略，支持审批缓存和审计日志
- 🛠️ **工具生态**: 内置工具 + MCP 协议支持
- 📊 **性能监控**: OpenTelemetry 集成，指标导出到 Prometheus/Grafana
- 🔒 **安全加固**: 路径遍历防护、密钥脱敏、审计日志

---

## 🚀 快速开始

### 前置要求

| 依赖 | 版本 | 说明 |
|------|------|------|
| **Python** | 3.12.x | 由 uv 自动管理 |
| **uv** | ≥ 0.4 | 快速依赖管理器 |
| **Anthropic API Key** | - | [获取 API Key](https://console.anthropic.com/) |

### 安装

```bash
# 1. 安装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 克隆项目
git clone https://github.com/youngyangyang04/KamaClaude.git
cd KamaClaude

# 3. 安装依赖
make install

# 4. 配置环境变量
cp .env.example .env
# 编辑 .env，填入你的 ANTHROPIC_API_KEY

# 5. 启动守护进程
make dev
```

### 验证安装

在新终端中：

```bash
# 测试连通性
uv run kama ping

# 启动 TUI
uv run kama-tui
```

**详细指南**: [docs/getting-started/quickstart.md](docs/getting-started/quickstart.md)

---

## 📖 文档

### 用户文档
- **[快速开始](docs/getting-started/quickstart.md)** — 5 分钟入门指南
- **[RUNBOOK.md](RUNBOOK.md)** — 完整操作参考：配置、命令、故障排查

### 开发文档
- **[架构概览](docs/architecture/overview.md)** — 系统设计与核心模块
- **[API 参考](docs/api/protocol.md)** — JSON-RPC 协议文档
- **[WIRE_PROTOCOL.md](WIRE_PROTOCOL.md)** — 自动生成的完整协议定义
- **[贡献指南](CONTRIBUTING.md)** — 开发环境设置、代码规范、提交流程

### 项目指导
- **[CLAUDE.md](CLAUDE.md)** — Claude Code 开发指南
- **[AGENT.md](AGENT.md)** — Agent 开发参考

---

## 🏗️ 架构

```
┌─────────────────────────────────────────────┐
│         Client Layer                        │
├───────────────────┬─────────────────────────┤
│   kama-tui (TUI)  │    kama (CLI)           │
└─────────┬─────────┴─────────┬───────────────┘
          │  JSON-RPC 2.0     │
          │  (TCP/NDJSON)     │
          v                   v
┌─────────────────────────────────────────────┐
│        kama-core (Daemon)                   │
├─────────────────────────────────────────────┤
│  • Session Manager   • LLM Provider         │
│  • Tool Registry     • Permission Manager   │
│  • Memory Store      • RAG Index            │
│  • Event Bus         • MCP Integration      │
└────────────┬────────────────────────────────┘
             │
             v
   ┌─────────────────────┐
   │  External Services  │
   ├─────────────────────┤
   │  • Anthropic API    │
   │  • Qdrant (Vector)  │
   │  • MCP Servers      │
   └─────────────────────┘
```

**核心特性：**
- 客户端无状态，所有状态在 daemon 中管理
- 支持多个并发会话（TUI + CLI 同时使用）
- 客户端可随时断开重连，会话持续存在

---

## 🛠️ 开发工具

```bash
# 代码质量
make format              # 自动格式化代码
make lint                # 运行 linter 和类型检查
make security-check      # 安全扫描（bandit + pip-audit）

# 测试
make test                # 运行所有测试
make test-unit           # 仅单元测试（快速）
make coverage            # 生成覆盖率报告

# 文档
make docs                # 生成协议文档

# Docker
make docker-build        # 构建 Docker 镜像
make docker-up           # 启动服务（kama-core + Qdrant）

# 清理
make clean               # 清理缓存和临时文件
```

**完整命令列表**: `make help`

---

## 🧪 测试

```bash
# 单元测试（无外部依赖，快速）
make test-unit

# 集成测试（需要 Anthropic API key）
export ANTHROPIC_API_KEY=sk-ant-...
make test-integration

# 覆盖率报告
make coverage
open htmlcov/index.html
```

**当前覆盖率**: 目标核心模块 ≥80%

---

## 🐳 Docker 部署

```bash
# 方式 1: Docker Compose（推荐）
docker-compose up -d

# 方式 2: 仅构建镜像
docker build -t kamaclaude:latest .
docker run -p 7437:7437 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  kamaclaude:latest
```

**服务包含**:
- `kama-core`: 守护进程（端口 7437）
- `qdrant`: 向量数据库（端口 6333）

---

## 🤝 贡献

欢迎贡献！请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 了解：

- 开发环境设置
- 代码规范（Ruff + Mypy + 中文注释规范）
- 测试要求
- 提交流程

**快速开始贡献**:

```bash
# 安装开发依赖和 pre-commit hooks
make install
make pre-commit

# 运行质量检查
make lint
make test-unit
```

---

## 📊 性能验证

2026-10-14 的离线基准：1000 次运行 P95 为 80.14 ms；32 个客户端的
1024 次认证 IPC 请求 P95 为 2.48 ms。此结果不包含真实模型、外网或 RAG 服务延迟。
生产准入状态与剩余工作见 [升级验收记录](docs/production/upgrade-2026-10-14.md)。

---

## 🔒 安全

- **路径遍历防护**: 所有文件操作验证路径安全性
- **密钥脱敏**: 日志中自动脱敏 API key
- **审计日志**: 记录所有权限决策和敏感操作
- **依赖扫描**: CI 中运行 `pip-audit` 和 `bandit`

发现安全问题？请使用仓库维护者提供的私密报告渠道，避免公开敏感信息。

---

## 📄 许可协议

本项目采用 [MIT License](LICENSE) 开源。

---

## 🙏 致谢

- [Anthropic](https://www.anthropic.com/) - Claude API
- [Textual](https://textual.textualize.io/) - TUI 框架
- [Qdrant](https://qdrant.tech/) - 向量数据库
- [uv](https://docs.astral.sh/uv/) - 快速依赖管理

---

<div align="center">

**[⬆ 回到顶部](#kamaclaude)**

Made with ❤️ by the KamaClaude Team

</div>

## 上下文工程升级（开发中）

已加入请求预算、工具结果外置、滚动投影，以及 P1/P2 的指令来源、会话记忆、
长期记忆时效、子任务交接、稳定缓存分段和工具按需发现。
TUI 使用 `/context` 查看来源与投影，`/notes` 查看脱敏笔记，`/compact 关注点` 手动压缩。
新能力各有独立开关且默认关闭，预检默认 shadow；最终 Linux 验收仍未完成。

- 架构：`docs/architecture/context-engineering.md`
- P1/P2：`docs/architecture/context-engineering-p1-p2.md`
- 配置示例：`docs/production/context-compaction.example.toml`
- 验证与已知限制：`docs/evaluation/context-benchmark.md`
- 迁移/回滚：`docs/production/context-upgrade-runbook.md`


安全升级配置、兼容性变更与已验证边界见 [安全实施报告](docs/production/security-implementation-2026-10-15.md)。
