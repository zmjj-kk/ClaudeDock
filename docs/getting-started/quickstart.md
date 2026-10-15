# KamaClaude 快速开始

本指南将帮助你在 5 分钟内启动并运行 KamaClaude。

## 前置要求

- **操作系统**: macOS / Linux / Windows (WSL2)
- **Python**: 3.12.x
- **uv**: ≥ 0.4（自动管理 Python 版本和依赖）

---

## 安装步骤

### 1. 安装 uv

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

验证安装：
```bash
uv --version  # 应输出 ≥ 0.4
```

### 2. 克隆项目

```bash
git clone https://github.com/YOUR_USERNAME/KamaClaude.git
cd KamaClaude
```

### 3. 安装依赖

```bash
# 安装所有依赖（自动创建虚拟环境）
uv sync

# 或者只安装运行时依赖（不含开发工具）
uv sync --no-dev
```

### 4. 配置环境变量

```bash
# 复制环境变量模板
cp .env.example .env

# 编辑 .env 文件，填入你的 API key
nano .env  # 或使用你喜欢的编辑器
```

**必需配置**：
```bash
ANTHROPIC_API_KEY=sk-ant-your-api-key-here
```

**可选配置**：
```bash
KAMA_PORT=7437              # 守护进程端口
KAMA_LOG_LEVEL=INFO         # 日志级别
KAMA_LLM_DEFAULT_MODEL=claude-sonnet-4-6  # 默认模型
```

### 5. 启动守护进程

```bash
# 前台启动（查看日志）
uv run kama-core

# 或后台启动
uv run kama-core &
```

看到以下输出表示启动成功：
```
level=INFO ts=2024-10-09T10:00:00 source=kama_claude.core.app msg="Server started on 127.0.0.1:7437"
```

### 6. 验证连接

打开新终端窗口：

```bash
# 测试连通性
uv run kama ping
# 输出: {"status": "ok", "server_version": "0.0.1", ...}

# 查看版本
uv run kama --version
# 输出: KamaClaude 0.0.1
```

---

## 使用 TUI（推荐）

KamaClaude 的主要用户界面是 TUI（Terminal UI）：

```bash
uv run kama-tui
```

**快捷键**：
- `Ctrl+C`: 发送消息
- `Ctrl+D`: 退出
- `↑/↓`: 浏览历史消息

---

## 第一次对话

### 使用 TUI

```bash
uv run kama-tui
```

输入：
```
你好，请列出当前目录的文件
```

LLM 将自动调用 `list_dir` 工具并返回结果。

### 使用 CLI（快速测试）

```bash
uv run kama run "列出当前目录的文件"
```

---

## Docker 快速启动（可选）

如果你更喜欢容器化部署：

```bash
# 启动所有服务（kama-core + Qdrant）
docker-compose up -d

# 查看日志
docker-compose logs -f kama-core

# 测试连接
docker exec kama-core kama ping

# 停止服务
docker-compose down
```

---

## 常见问题

### Q: 端口 7437 被占用

**解决方法 1**：更改端口
```bash
export KAMA_PORT=8000
uv run kama-core
```

**解决方法 2**：停止占用端口的进程
```bash
# 查找进程
lsof -i :7437

# 停止进程
kill -9 <PID>
```

### Q: `ANTHROPIC_API_KEY` 未设置

确保你已经：
1. 复制了 `.env.example` 到 `.env`
2. 在 `.env` 中填入了有效的 API key
3. 启动 `kama-core` 时加载了 `.env`（自动加载）

### Q: 权限请求如何响应？

当 LLM 尝试执行敏感操作时（如读取文件、执行命令），会弹出权限请求：

```
Permission request: bash
Command: ls -la
[A]llow / [D]eny / [Always] / [Never]: a
```

- `A`: 仅本次允许
- `D`: 仅本次拒绝
- `Always`: 本会话内总是允许
- `Never`: 总是拒绝（持久化）

### Q: 如何查看详细日志？

```bash
# 启用 DEBUG 级别日志
KAMA_LOG_LEVEL=DEBUG uv run kama-core

# 或写入文件
KAMA_LOG_FILE=~/.kama/logs/debug.log uv run kama-core
```

---

## 下一步

- 📖 阅读 [架构文档](../architecture/overview.md) 了解系统设计
- 🛠️ 查看 [开发指南](../CONTRIBUTING.md) 开始贡献
- 📝 浏览 [RUNBOOK.md](../RUNBOOK.md) 了解完整操作参考

---

## 获取帮助

- 🐛 遇到问题？[提交 Issue](https://github.com/YOUR_USERNAME/KamaClaude/issues)
- 💬 技术讨论？[加入 Discussions](https://github.com/YOUR_USERNAME/KamaClaude/discussions)
- 📧 安全问题？发送邮件至 security@example.com

祝使用愉快！🎉
