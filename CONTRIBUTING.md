# 贡献指南

感谢你对 KamaClaude 的关注！本指南将帮助你快速上手项目开发。

## 目录

- [开发环境设置](#开发环境设置)
- [代码规范](#代码规范)
- [提交流程](#提交流程)
- [测试要求](#测试要求)
- [文档规范](#文档规范)
- [问题报告](#问题报告)

---

## 开发环境设置

### 前置要求

| 依赖 | 版本 | 说明 |
|------|------|------|
| **Python** | 3.12.x | 严格要求 3.12 小版本 |
| **uv** | ≥ 0.4 | 快速依赖管理器 |
| **Git** | ≥ 2.0 | 版本控制 |
| **Docker** | 可选 | 用于容器化测试 |

### 安装步骤

```bash
# 1. Fork 并克隆仓库
git clone https://github.com/YOUR_USERNAME/KamaClaude.git
cd KamaClaude

# 2. 安装 uv（如果尚未安装）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 3. 安装所有依赖（包括开发依赖）
make install

# 4. 安装 pre-commit hooks（推荐）
make pre-commit

# 5. 复制环境变量模板
cp .env.example .env
# 编辑 .env 文件，填入你的 ANTHROPIC_API_KEY

# 6. 验证安装
make test-unit
```

---

## 代码规范

### Python 代码风格

我们使用严格的代码质量工具链：

- **Ruff**: 快速 linter + formatter（替代 black、isort、flake8）
- **Mypy**: 严格类型检查（`--strict` 模式）
- **行宽**: 100 字符（`pyproject.toml` 配置）

```bash
# 自动格式化代码
make format

# 运行 linter 和类型检查
make lint
```

### 注释规范

**关键约定**：所有函数必须有**单行中文注释**紧接在 `def` 之前：

```python
# 发送 JSON-RPC 响应并刷新写缓冲区
async def send_response(writer: StreamWriter, data: dict) -> None:
    ...
```

**测试函数**需要**两行注释**：

```python
# 功能：验证 publish 后订阅者能收到事件对象
# 设计：用内联 handler 收集事件引用，断言 is 而非 ==，排除序列化中间步骤的干扰
async def test_publish_reaches_subscriber() -> None:
    ...
```

- **功能行**：说明"测什么"（验证的具体行为或不变式）
- **设计行**：说明"为什么这样测"（覆盖的边界条件、选择此测试方式的理由）

### 类型注解

严格要求类型注解：

```python
# ✅ 正确
def process_message(data: dict[str, Any]) -> Result | None:
    ...

# ❌ 错误（缺少类型注解）
def process_message(data):
    ...
```

### 异常处理

使用 `src/kama_claude/core/errors.py` 中定义的具体异常类，避免宽泛的 `except Exception`：

```python
# ✅ 正确
from kama_claude.core.errors import ToolExecutionError, ToolNotFoundError

try:
    tool = registry.get(name)
except KeyError:
    raise ToolNotFoundError(name)

# ❌ 错误（宽泛捕获）
try:
    tool = registry.get(name)
except Exception:
    pass
```

---

## 提交流程

### 分支策略

```
main            # 稳定版本
  ├── stage/s*  # 阶段开发分支（s0、s1、s2...）
  └── feature/* # 功能分支
  └── fix/*     # 修复分支
```

### 提交信息规范

遵循 [Conventional Commits](https://www.conventionalcommits.org/) 规范：

```
<type>(<scope>): <subject>

<body>

<footer>
```

**Type 类型：**

- `feat`: 新功能
- `fix`: 修复 bug
- `docs`: 文档变更
- `style`: 代码格式（不影响功能）
- `refactor`: 重构（不改变功能）
- `test`: 添加或修改测试
- `chore`: 构建工具、依赖更新

**示例：**

```
feat(tools): 添加文件搜索工具 glob_search

实现基于 glob 模式的文件搜索工具，支持递归匹配和排除模式。

Closes #42
```

### 提交前检查清单

在提交之前，确保：

- [ ] 代码通过 `make lint`
- [ ] 所有测试通过 `make test-unit`
- [ ] 新功能有对应的单元测试
- [ ] 更新了相关文档
- [ ] 提交信息符合规范
- [ ] pre-commit hooks 通过（如已安装）

### Pull Request 流程

1. **创建分支**
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **开发与测试**
   ```bash
   # 编写代码
   # ...

   # 运行测试
   make test-unit

   # 检查代码质量
   make lint
   ```

3. **提交变更**
   ```bash
   git add .
   git commit -m "feat(scope): your commit message"
   ```

4. **推送到 GitHub**
   ```bash
   git push origin feature/your-feature-name
   ```

5. **创建 Pull Request**
   - 在 GitHub 上创建 PR
   - 填写 PR 描述模板
   - 等待 CI 检查通过
   - 等待代码审查

---

## 测试要求

### 测试分类

```
tests/
├── unit/         # 单元测试（快速，无外部依赖）
├── integration/  # 集成测试（需要真实服务）
└── benchmark/    # 性能基准测试
```

### 测试覆盖率

- **核心模块** (`src/kama_claude/core/`) 要求 **≥80%** 覆盖率
- **新功能** 必须包含单元测试
- **Bug 修复** 应添加回归测试

```bash
# 生成覆盖率报告
make coverage

# 查看报告
open htmlcov/index.html
```

### 测试编写指南

**单元测试示例：**

```python
import pytest
from kama_claude.core.tools.registry import ToolRegistry

# 功能：验证注册同名工具时抛出异常
# 设计：先注册一个工具，再尝试注册同名工具，断言抛出 ValueError
def test_duplicate_tool_registration_raises() -> None:
    registry = ToolRegistry()
    registry.register("foo", lambda: None)

    with pytest.raises(ValueError, match="already registered"):
        registry.register("foo", lambda: None)
```

**异步测试示例：**

```python
# 功能：验证异步工具调用返回正确结果
# 设计：使用 pytest-asyncio 的 auto 模式，直接 await 工具调用
async def test_async_tool_execution() -> None:
    result = await some_async_function()
    assert result.status == "success"
```

---

## 文档规范

### 协议文档

`WIRE_PROTOCOL.md` 是**自动生成**的，请勿手动编辑。修改协议定义后运行：

```bash
make docs
```

### API 文档

公开的类和函数应包含 docstring（暂不强制要求，但推荐）：

```python
class ToolRegistry:
    """工具注册表：管理工具的注册、查询和调用"""

    def register(self, name: str, handler: Callable) -> None:
        """注册一个工具

        Args:
            name: 工具名称（唯一标识符）
            handler: 工具处理函数

        Raises:
            ValueError: 工具名已存在
        """
        ...
```

### 更新 CLAUDE.md

如果你的变更影响了架构或开发流程，请同步更新 `CLAUDE.md`。

---

## 问题报告

### Bug 报告

提交 Issue 时，请包含：

1. **环境信息**
   - 操作系统和版本
   - Python 版本
   - KamaClaude 版本

2. **复现步骤**
   - 详细的操作步骤
   - 最小化的复现代码

3. **预期行为 vs 实际行为**

4. **日志输出**
   ```bash
   # 启用详细日志
   KAMA_LOG_LEVEL=DEBUG uv run kama-core
   ```

### 功能请求

请在 Issue 中说明：

1. **使用场景**：为什么需要这个功能？
2. **期望行为**：功能应该如何工作？
3. **替代方案**：是否有其他解决方法？

---

## 安全问题

如果发现安全漏洞，**请勿公开提交 Issue**，而是通过以下方式报告：

- 发送邮件至：[security@example.com]
- 或通过 GitHub Security Advisories

---

## 许可协议

提交代码即表示你同意你的贡献将以 [MIT License](LICENSE) 发布。

---

## 需要帮助？

- 📖 查看 [RUNBOOK.md](RUNBOOK.md) 了解操作指南
- 💬 加入 Discussions 讨论技术问题
- 🐛 提交 Issue 报告 bug 或请求功能

感谢你的贡献！🎉
