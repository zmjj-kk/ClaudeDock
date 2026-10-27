# 并行工具执行实施记录

日期：2026-10-20。依据 `parallel-tool-execution-upgrade-plan.md` 实施。并行和 shadow 默认关闭，未更改用户的运行配置。

## 实现范围

- P0：`ToolExecutionScheduler` 使用 `TaskGroup` 管理有界批次；先验证非空且唯一的调用 ID，超出批次上限则整批返回配对错误。Loop 在写入 assistant 消息之前拒绝无效 ID，避免把错误协议交给下一轮模型。调用预算和停滞检测保留，结果由父任务按输入顺序统一写入上下文并持久化检查点。
- P1：`ResourceClaim` 和冲突 DAG 支持读写、写写、目录覆盖和规范化路径依赖。不确定资源作为屏障。仅精确类型的内置 `read_file`、`list_dir` 自动进入可信并发白名单；资源范围须通过 `SafePath`，硬链接和敏感路径继续拒绝。其他宿主实现需显式 `register(..., trusted_concurrency=True)`，并提供可信资源解析器。`write_file`、`bash`、子 Agent 启动、工具发现、MCP 和未知工具维持串行屏障。配置 `resource_scoped` 本身不会授予写工具并发权限。
- P2：所有调用仍经过 `invoke_tool`、SecurityGate、权限审批、超时、重试和审计。每 Run 与全局执行槽在审批通过之后取得，排队计入调用期限，获得槽后再次校验授权。审批队列独立有界，取消会回收 Future。Journal 的准备、状态更新和保存使用可重入锁，操作绑定 Run、调用 ID、参数摘要及工具身份；多调用审批状态集中汇总。已完成操作恢复原结果，未决或身份不一致的操作不重新执行。
- P2：工具任务不修改父级结果预算；收集器按输入顺序分配字节额度和外置原文。相同存储目录的服务实例共享锁，检查原文与元数据合计配额后提交。存储失败保留工具已完成状态与错误结果，恢复时不重放副作用；额度不足以保留完整引用时返回有界的省略状态。
- P3：增加 `tool.batch_planned`、`tool.batch_started`、`tool.batch_finished` 类型化事件和更新后的 `WIRE_PROTOCOL.md`。记录批次耗时、数量、实际执行峰值、排队时间、冲突串行、失败、取消及结果不匹配指标。事件分别报告在途生命周期数量和实际执行峰值，批次事件不携带工具参数或输出。提供 shadow 开关和可重复基准。

文件读取与目录枚举在 `asyncio.to_thread()` 中运行，执行槽限制实际在途 I/O。取消后先等待工作线程退出再释放槽；线程不能被强制终止，异常阻塞的文件系统仍可能延长取消与超时的实际返回时间。

## 配置与启用

```toml
[tool_execution]
parallel_enabled = false
shadow_enabled = false
policy = "safe_only"              # safe_only / resource_scoped
max_concurrency_per_run = 4
max_calls_per_batch = 8
max_pending_approvals = 8
```

配置由 `ClaudeDockConfig` 校验，经 `AgentRunner` 传到根运行及嵌套子 Agent。全局工具上限继续复用 `runtime.max_tool_requests`。审批数量上限作用于共享 PermissionManager；执行槽与待审批数量分别控制。

先保持 `parallel_enabled=false` 并设置 `shadow_enabled=true` 观察规划事件；确定适用工具和负载后，可设置 `parallel_enabled=true` 仅开放现有安全白名单。将其设回 false 即恢复串行调度。没有开启任何用户配置或生产灰度。

## 自动验证

```bash
.venv/bin/ruff check src tests scripts
.venv/bin/mypy src
.venv/bin/python scripts/gen_protocol_doc.py --check
.venv/bin/pytest tests/ -q
.venv/bin/pytest tests/benchmarks/test_parallel_tools.py -q -s
```

验证结果：全量 `tests/` **651 passed in 29.96s**；Ruff 检查通过，Mypy 的 158 个源码文件通过，协议文档一致性检查通过。全量运行曾出现一次既有 IPC 测试守护进程超过 3 秒启动门限；隔离复核通过（1 passed），随后完整重跑通过，未放宽启动门限。

新增测试覆盖逆序完成、屏障、冲突路径和别名、非法 ID、批次限额、配置校验、宿主信任边界、真实线程 I/O 重叠、共享执行槽、审批容量和取消、结果预算、磁盘配额、日志并发、恢复身份、超时、取消及上下文协议。

## 基准与灰度结论

本地 WSL / Ubuntu-22.04 / Python 3.12.13；每批四次调用、每模式七次采样，均走生产安全网关。原始数据见 `parallel-tool-execution-benchmark.json`。

| 样本 | 串行 P50 / P95（ms） | 并行 P50 / P95（ms） | 串行 / 并行调用吞吐（次/秒） |
| --- | --- | --- | --- |
| 模拟原生异步等待 20ms | 84.101 / 85.047 | 24.367 / 43.473 | 47.516 / 141.544 |
| 本地文件读取 4 × 256KiB | 10.070 / 11.087 | 17.458 / 18.608 | 399.585 / 226.545 |

所有样本错误数为零。模拟异步延迟体现了调度重叠能力；这组本地小文件读取并行反而更慢，线程调度、校验和本地 I/O 的成本可能超过重叠收益。样本较小、模式按固定顺序执行，没有代表性生产负载或远端网络测量，不能据此承诺端到端业务加速。保留默认关闭，是否扩大白名单由后续真实负载测量决定。

## 运行边界

- 只实现单进程宿主内并发治理；本地锁不提供多进程存储事务或外部业务的幂等账本。
- 未授予 MCP、Shell 或任意写操作并行权限；宿主扩展资源工具仍需审查实际访问与资源声明的一致性。
- 超时返回 `timeout` 并在 Journal 中记录 `unknown`；运行中取消同样保留未决状态。它们不代表副作用没有发生。
- 老日志若缺少工具身份，执行网关拒绝把它直接作为新实现的已完成结果恢复，需先核查操作。
- 生产灰度、真实检索/网络负载压测和长期 P50/P95、吞吐及存储指标观察尚未执行。基准数据仅用于本地验收和决定是否灰度。
