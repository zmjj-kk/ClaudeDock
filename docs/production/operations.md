# 升级、备份与回滚

## 本机启动

```bash
uv sync --frozen
# 安装 bubblewrap、util-linux；确认目标机器允许非特权 user namespaces
# Ubuntu/Debian 无需系统安装时可执行 bash scripts/install_local_sandbox.sh
uv run claudedock-core
uv run claudedock ping
uv run claudedock-tui
```

默认本机认证 token 自动生成。旧客户端须同步升级；禁止复制 token 到日志。
没有模型 key 时健康探针仍可使用，发起模型任务会明确失败。
授权客户端能看到任务内容，因此仍须保护本地用户身份与凭证。

## 容器构建

```bash
docker build -t claudedock:<release-tag> .
# 下载网络不可靠时，先在可信机器准备锁定的 wheelhouse
bash scripts/prepare_container_wheels.sh
docker build --build-arg OFFLINE_BUILD=1 -t claudedock:<release-tag> .
docker run -d --name claudedock-probe claudedock:<release-tag>
docker exec claudedock-probe python -m claudedock.core.transport.healthcheck
docker logs claudedock-probe
docker rm -f claudedock-probe
```

离线路径仅离线安装 Python 运行依赖；基础镜像、uv 构建层与 apt 仍需下载或缓存。
build-assets/wheels 不进版本库，运行依赖要求锁文件中版本与哈希匹配。
使用非 root UID 10001；workspace 绑定卷须允许该身份写入。
默认容器 seccomp/user namespace 策略可能禁止嵌套 bubblewrap：Shell 会拒绝执行。
上线前必须在目标平台验证 shell 隔离；不能通过禁用沙箱或放开 privileged 规避。

## Compose 与远程 IPC

```bash
# 分别生成两个随机凭证，保存到私密 .env，不要复用
python -c 'import secrets; print(secrets.token_urlsafe(32))'
# 设置 CLAUDEDOCK_IPC_TOKEN 和 QDRANT_API_KEY 后：
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
```

主机端口只绑定 127.0.0.1；Qdrant 只走 Compose 网络并使用 API key。
跨主机接入配置 CLAUDEDOCK_ALLOW_REMOTE=1、CLAUDEDOCK_TLS_CERT、CLAUDEDOCK_TLS_KEY；
客户端配置可信 CLAUDEDOCK_TLS_CA 与必要的 CLAUDEDOCK_TLS_ENABLE=1。
CLAUDEDOCK_TRUSTED_PROXY=1 仅用于受控网络及具备认证/TLS 的代理，不能当作公网安全机制。

## 资源预算与费用

`[runtime]` 和 `CLAUDEDOCK_RUNTIME_<字段大写>` 支持覆盖：max_runs=8、max_jobs=32、
max_llm_requests=8、max_tool_requests=16、max_connections=64、max_requests=32、
request_timeout_s=3600、run_timeout_s=1800、max_model_calls=128、max_tool_calls=256、
max_tokens=1000000、stagnation_limit=3、max_cost_usd=0。
0 费用上限表示未启用；启用前在 llm 配置里填写当前价格，未知价格会在调用前拒绝。
价格用于估算，无法证明超时/失联请求没有计费；父子运行共享预算。

## 离线备份与恢复

先停止 daemon、客户端写入者以及 MCP/外部写入者。备份工具会拒绝已被 daemon
锁持有的 sessions 目录，但无法检测任意第三方写入者。
完整保存 ~/.claudedock 及自定义 sessions_dir、memory SQLite、任务目录；RAG/向量
派生数据可重新生成，远程 Qdrant 如需保留还须使用其原生快照流程。

```bash
uv run python scripts/state_backup.py backup ~/.claudedock /secure/backups/claudedock-<timestamp>
uv run python scripts/state_backup.py verify /secure/backups/claudedock-<timestamp>
# 恢复到不存在的新目录，避免覆盖当前状态
uv run python scripts/state_backup.py restore /secure/backups/claudedock-<timestamp> /secure/recovery/claudedock
```

备份目录/文件为 0700/0600，含原始对话与凭证，必须放在加密的受控存储。
禁止备份目录位于源目录内；拒绝 symlink/特殊文件。SQLite 使用 backup API，
不依赖直接复制 WAL 文件。恢复前逐文件校验 SHA256。
本次隔离夹具完成 SQLite WAL 与会话文件的备份/恢复核对；用户真实数据未被切换。

## 发布与回滚

1. 发布前保存原镜像 digest、Git SHA、完整状态快照和配置，完成所有 CI 门禁。
2. 在测试目录恢复备份，执行认证 ping、会话恢复、审批、工具隔离和取消验证。
3. 排空服务并停止写入，备份生产状态，切换新镜像和配套客户端。
4. 观察健康、persistence_error、unknown operations、请求队列与延迟。
5. 失败时停止新版本，将升级前状态恢复到新目录，指向旧镜像及旧配置再启动。
   不允许新旧 daemon 同时管理同一 sessions 目录；不能用旧软件直接重放新 unknown 操作。
6. 对 unknown 外部副作用检查真实文件/外部系统，再决定补偿或重新执行。

旧版 IPC 没有本轮的认证保障，回滚时必须继续保留外层访问控制，且不能公开端口。
本次未替维护者执行生产发布，也没有对用户原始未提交代码做合并或提交。

## 留存策略

Trace 自动轮转，其他会话/事件/checkpoint/任务文件没有自动 TTL 或全局磁盘配额。
部署时先确定保留期限与数据主体删除流程，停止服务后备份，再按明确 session/run
目录处理。不要清理存在 unknown 操作的检查点；派生索引清理后须先验证可重建。

## 验证命令

```bash
uv run ruff check src tests scripts
uv run mypy src
uv run pytest tests/unit --cov=src/claudedock --cov-report=term
uv run pytest tests/integration -m 'not integration'
uv run python scripts/gen_protocol_doc.py --check
uv export --frozen --no-dev --no-emit-project --output-file runtime-requirements.txt
uv run pip-audit -r runtime-requirements.txt --disable-pip --require-hashes
uv run bandit -r src -ll
uv run python scripts/production_benchmark.py --help
```

CI 65% 覆盖率门槛用于防止继续下降，生产准入仍按计划的建议 80%/90% 审核。
OTel 默认为关闭；需安装 opentelemetry-sdk/opentelemetry-exporter-otlp，并配置接收端，
OTEL_ENABLED=true 才启用。未安装时不会阻止本地运行。
