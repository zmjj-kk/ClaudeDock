.PHONY: help install lint test test-unit test-integration coverage docs verify-s0 clean dev benchmark security-check docker-build docker-up docker-down format pre-commit

# 默认目标：显示帮助信息
help:
	@echo "KamaClaude 开发工具链"
	@echo ""
	@echo "安装与配置："
	@echo "  make install          安装所有依赖（包括开发依赖）"
	@echo "  make pre-commit       安装 pre-commit hooks"
	@echo ""
	@echo "代码质量："
	@echo "  make format           自动格式化代码（ruff format）"
	@echo "  make lint             运行 linter 和类型检查"
	@echo "  make security-check   运行安全扫描（bandit + pip-audit）"
	@echo ""
	@echo "测试："
	@echo "  make test             运行所有测试（单元 + 集成）"
	@echo "  make test-unit        仅运行单元测试"
	@echo "  make test-integration 仅运行集成测试"
	@echo "  make coverage         生成测试覆盖率报告（HTML）"
	@echo "  make benchmark        运行性能基准测试"
	@echo ""
	@echo "文档："
	@echo "  make docs             生成协议文档（WIRE_PROTOCOL.md）"
	@echo ""
	@echo "开发与部署："
	@echo "  make dev              启动开发环境（daemon + 自动重启）"
	@echo "  make docker-build     构建 Docker 镜像"
	@echo "  make docker-up        启动 Docker Compose 服务"
	@echo "  make docker-down      停止 Docker Compose 服务"
	@echo ""
	@echo "清理："
	@echo "  make clean            清理缓存和临时文件"
	@echo ""
	@echo "CI 验证："
	@echo "  make verify-s0        运行 S0 阶段的完整验证流程"

# 安装所有依赖（包括开发依赖）
install:
	uv sync

# 安装 pre-commit hooks
pre-commit:
	uv run pre-commit install

# 自动格式化代码
format:
	uv run ruff format src tests scripts
	uv run ruff check --fix src tests scripts

# Lint 和类型检查
lint:
	uv run ruff check src tests scripts
	uv run mypy src

# 运行所有测试
test:
	uv run pytest tests/ -v

# 仅运行单元测试（快速反馈）
test-unit:
	uv run pytest tests/unit -v

# 仅运行集成测试
test-integration:
	uv run pytest tests/integration -v

# 生成测试覆盖率报告（HTML + 终端输出）
coverage:
	uv run pytest tests/unit -v --cov=src/kama_claude --cov-report=html --cov-report=term
	@echo ""
	@echo "覆盖率报告已生成：htmlcov/index.html"

# 运行性能基准测试
benchmark:
	@echo "运行性能基准测试..."
	@uv run pytest tests/benchmark -v --benchmark-only 2>/dev/null || echo "尚未实现 benchmark 测试"

# 安全扫描
security-check:
	@echo "运行 Bandit 安全扫描..."
	@uv run bandit -r src/ -ll
	@echo ""
	@echo "运行 pip-audit 依赖漏洞检查..."
	@uv run pip-audit

# 生成协议文档
docs:
	uv run python scripts/gen_protocol_doc.py

# 启动开发环境（守护进程 + 自动重启）
dev:
	@echo "启动 kama-core 守护进程（按 Ctrl+C 停止）..."
	@uv run kama-core

# Docker 构建
docker-build:
	docker build -t kamaclaude:latest .

# Docker Compose 启动
docker-up:
	docker-compose up -d
	@echo "服务已启动，查看日志：docker-compose logs -f"

# Docker Compose 停止
docker-down:
	docker-compose down

# 清理缓存和临时文件
clean:
	@echo "清理 Python 缓存..."
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null
	find . -type f -name "*.pyc" -delete 2>/dev/null
	find . -type f -name "*.pyo" -delete 2>/dev/null
	@echo "清理构建产物..."
	rm -rf build/ dist/ *.egg-info/ 2>/dev/null
	@echo "清理测试缓存..."
	rm -rf .pytest_cache/ .mypy_cache/ .ruff_cache/ htmlcov/ .coverage 2>/dev/null
	@echo "清理 RAG 索引缓存..."
	rm -f .rag_index_cache.json .rag_index_manifest.json 2>/dev/null
	@echo "清理完成！"

# S0 阶段完整验证（用于 CI）
verify-s0:
	uv sync --frozen
	uv run ruff check src tests scripts
	uv run mypy src
	uv run pytest tests/unit -v
	uv run pytest tests/integration -k ping -v
	uv run python scripts/gen_protocol_doc.py --check
