# ClaudeDock 并行工具调用生产级升级方案

> 状态：调度基础与安全保护已实施，生产并行默认关闭；当前验证与运行边界见 [实施记录](parallel-tool-execution-implementation.md)
> 适用项目：ClaudeDock（Python 3.12 / Asyncio / Claude API）
> 编写日期：2026-10-20
> 目标：实现安全、可观测、可取消、可恢复的冲突感知并行工具执行（Conflict-Aware Parallel Tool Execution）

## 1. 背景与目标

当前 `AgentLoop` 已能接收模型在一次响应中生成的多个 `tool_calls`，但在 `src/claudedock/core/loop.py` 中通过 `for + await invoke_tool()` 依次执行，普通工具调用在单轮内串行。

现有 `spawn_agent(run_in_background=True)` 属于**子 Agent 级并发**：通过 `RunSupervisor.spawn()` / `asyncio.create_task()` 启动独立 Agent Loop，并通过 `agent_result(run_id)` 按需查询。它与单轮 Tool Calls 并行是两种机制，不应混为一谈。

本次改造目标：

1. 对同一 LLM 响应中的独立 Tool Calls 进行安全、有界并发执行。
2. 由宿主调度器决定并发策略，而不是仅凭模型声称“这些调用可以并行”。
3. 不绕过现有 `SecurityGate`、`PermissionManager`、`RunJournal`、工具超时/重试及审计。
4. 按原始 `tool_use_id` 与生成顺序统一提交 `tool_result`，保证上下文协议完整。
5. 对超时、取消、崩溃和重复操作保留保守恢复语义。
6. 通过可重复的实验评估延迟、吞吐量、正确性与安全回归；不提前宣称性能收益。

**非目标（第一阶段）**：引入 Redis/Celery/分布式任务队列；默认并行任意 Shell/MCP 写操作；将普通工具改为后台任务并交给 LLM 轮询；重新实现整个 Agent Runtime。

## 2. 基于现有源码的差距分析

- `core/loop.py`：目前每轮按顺序调用 `invoke_tool()`；`tool_slots=asyncio.Semaphore(16)` 仅约束跨任务共享执行槽，不会让串行循环自动并发。
- `core/tools/base.py`：已有 `risk_level`、`capabilities`、`idempotent` 与 `retryable_errors`，缺少可信的并发/资源声明。
- `core/tools/invocation.py`：工具调用已经接入参数校验、安全门禁、权限审批、限时执行、重试和 Journal；并行调度必须复用这一入口。
- `core/journal.py`：同一 Run 的多个操作写入同一 `checkpoint.json`，需要定义并发安全的状态迁移和持久化边界。
- `core/compact/tool_result_store.py`：`current_result_budget` 在新 Task 中继承同一个可变 `ResultBudget` 对象；并发执行阶段直接扣减 `remaining` 可能导致预算竞争。结果外置存储的配额检查也需并发治理。
- `core/permissions/manager.py`：权限审批可能挂起，不能让等待审批占满实际工具执行槽；不同调用的审批必须严格绑定身份与参数。
- `core/context.py`：`add_tool_result()` 已支持同轮结果合并；并发 Task 不能直接修改 `messages`，应交由 Collector 集中提交。
- `core/supervisor.py`：继续管理 Run 和后台子 Agent；普通工具批次采用结构化并发，不复用 `agent_result`。
- `core/events/bus.py`：现有事件总线与监控可扩展批次调度事件和指标。

**性能前提**：`read_file`、`list_dir` 等 `async def invoke()` 内仍包含同步文件系统操作。即便创建多个 Task，纯同步片段不会自动并行；若要获得文件 I/O 的实际重叠，需要评估并有界使用 `asyncio.to_thread()`，并确保安全路径校验和文件访问语义不变。真正原生异步的检索或网络工具可先作为延迟测试样本，但是否开放并发仍取决于可信安全声明。

## 3. 目标架构与职责边界

```text
LLM Response: tool_use[A, B, C, ...]
          │
          ▼
ToolExecutionScheduler
  ├─ Batch Validator：ID 唯一性、调用预算、工具身份、参数
  ├─ Policy Resolver：受信工具并发元数据 / 默认串行
  ├─ Conflict Planner：资源读写冲突及顺序依赖
  ├─ Batch Executor：TaskGroup + 单 Run / 全局 Semaphore
  │    └─ invoke_tool()：SecurityGate / 审批 / Journal / 超时 / 重试
  └─ Result Collector：预算集中处理 / 按原顺序提交 / Trace
          │
          ▼
ExecutionContext.add_tool_result()（由 AgentLoop 串行调用）
          │
          ▼
下一轮 LLM（本轮结果收齐后才允许发起）
```

**职责划分**：

- LLM：生成本轮工具调用及参数；可表达任务意图，但不能授予安全特权。
- Scheduler：决定允许哪些调用并发、哪些调用排队，维护批次生命周期。
- `invoke_tool()`：继续作为唯一工具执行网关；安全规则和审计不下沉到模型。
- Collector：确保一调用对应一结果、结果有界、顺序确定且消息结构合法。
- RunSupervisor：统一管理 Run/后台 Agent；全局资源限制需能覆盖工具执行。
- RunJournal / 业务后端：保留操作状态和防重复副作用语义。

## 4. 并发决策策略

### 4.1 首版采用可信白名单 + 保守串行

新增可信工具元数据，推荐默认值：

```python
from typing import Literal

ConcurrencyPolicy = Literal["parallel_read", "resource_scoped", "serialized"]

class BaseTool(ABC):
    concurrency_policy: ConcurrencyPolicy = "serialized"
```

推荐第一阶段策略：

- `read_file`、`list_dir`：在权限、路径及实现验证通过后允许只读并发。
- `write_file`：默认串行；后续可按规范化文件资源声明启用不同路径并发。
- `bash`：默认串行；任意命令含隐式读写及环境副作用，不能只靠字符串粗判。
- `spawn_agent`：保留当前后台子 Agent 流程，并视为当前批次调度屏障。
- `agent_result`、`tool_search`：默认串行；查询/发现过程可能修改 Registry 或任务注册表。
- 未经可信注册的 MCP 工具、动态插件、未知工具：默认串行或按现有权限策略拒绝。
- 其他工具（RAG、记忆、任务管理等）：逐一审核其存储、锁、连接池及资源语义后再加入白名单。

注意：`idempotent=True` 不能推导出“可以并发”；“只读”也不意味着可以与同一资源的写操作并发。远端 MCP 的只读/幂等提示不能单独成为授权依据。

### 4.2 保留调用顺序的冲突依赖

后续引入可信 `ResourceClaim(resource, mode)`，其中 mode 为 `read | write`：

```text
read_file("a.py")  → READ:file:/workspace/a.py
read_file("b.py")  → READ:file:/workspace/b.py
write_file("a.py") → WRITE:file:/workspace/a.py
```

规则：

1. 读-读且不存在其他共享状态冲突：可并发。
2. 读-写或写-写且资源范围重叠：按原始调用顺序执行。
3. 无法准确推导资源范围：按串行屏障处理。
4. 路径必须经过现有 `SafePath` 规范化和授权；目录范围、符号链接、硬链接、别名、子路径覆盖等需保守处理。
5. 写操作及不透明外部操作默认不能被“无冲突”声明自动放行；允许的并发策略仅能来自可信宿主实现。
6. 严禁模型通过传入额外的 `parallel=true` 等参数绕过宿主判断。

建议先实现**连续安全只读批次并行 + 非只读屏障**，确认正确性后再实现以工具调用为节点、冲突为有向边的 DAG 调度。这样可保持执行顺序等价性，避免批次内未知副作用产生非预期重排。

## 5. 结构化并发执行

Python 3.12 推荐使用 `asyncio.TaskGroup` 管理一轮中已允许并发的调用，结合有界信号量控制资源：

```python
import asyncio

class ToolExecutionScheduler:
    def __init__(self, invoke_one, global_slots, per_run_slots):
        self._invoke_one = invoke_one
        self._global_slots = global_slots
        self._per_run_slots = per_run_slots

    async def _run_one(self, call):
        async with self._per_run_slots:
            async with self._global_slots:
                try:
                    return await self._invoke_one(call)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    return ToolResult(
                        content=f"Tool failed: {type(exc).__name__}",
                        is_error=True,
                        error_type="runtime_error",
                    )

    async def execute_parallel(self, calls):
        async with asyncio.TaskGroup() as group:
            tasks = [
                group.create_task(self._run_one(call), name=f"tool:{call.id}")
                for call in calls
            ]
        return [task.result() for task in tasks]
```

以上**仅是调度骨架**，不能直接替换真实调用链；生产实现应使用当前 `invoke_tool()`，并在统一安全审核、审批和结果后处理重构后确定信号量边界。

关键语义：

- 同轮 Tool Calls 可以异步执行，**但结果不通过** `agent_result()` **轮询**。
- 普通工具的运行周期受当前批次控制，全部结果收齐才进入下一轮 LLM。
- 独立工具错误作为其对应的 `ToolResult(is_error=True)` 记录；取消应继续传播。
- 超时或取消期间副作用可能已发生，不能视为未执行；应记录 `unknown_outcome` 并按 Journal/业务后端规则核查。
- 同时限制每 Run 的并发量、全局工具槽数、每批次数量和等待队列容量，避免创建海量等待中的 Task。
- TaskGroup 不能强行终止所有阻塞线程/已发出的外部请求；需要工具自身提供协作取消和资源回收能力。

## 6. 与既有机制的集成

### 6.1 ToolRegistry 与工具元数据

- 在 `BaseTool` 添加默认串行的可信并发策略。
- 在 `ToolRegistry` 注册时冻结/校验并发元数据，不接受模型或未知 MCP 服务器的自报值作为安全事实。
- 资源声明从经过验证的参数中推导，不直接相信模型提供的“资源列表”。
- 未来可为不同文件的受信读/写操作逐步开放资源级并发。

### 6.2 RunJournal、操作 ID 和崩溃恢复

- 继续采用稳定 `operation_id = run_id + ":" + tool_use_id`。
- 调度批次创建时先验证所有 `tool_use_id` 非空且不重复；重复项应拒绝执行并输出协议错误。
- `prepare` / `update` 的读改写必须具备原子保护；单事件循环下同步代码目前不会在不含 `await` 的片段中切走，但升级后增加线程、进程或异步持久化时需明确使用专用锁和存储事务。
- 不持有 Journal 锁等待网络调用、工具执行或人工审批，否则并发退化为串行。
- 记录 `prepared → executing → completed/failed/unknown`；对于未决结果不盲目重复执行。
- 同一操作 ID、工具身份及参数摘要必须一致；避免并发重试、恢复重放与新操作混淆。
- 本地 Journal 不能替代外部业务后端的原子幂等账本。

### 6.3 权限审批与安全门禁

- 每个 Tool Call 依然通过 `SecurityGate` 和 `PermissionManager`，包括用户确认、工具自检、参数和作用域绑定。
- 将**等待审批**与**真正执行**分开治理：待审批任务有单独上限；审批中不占用执行信号量。
- 授权通过后执行前再次检查策略版本、参数摘要、工具身份、审批期限与调用资源范围。
- 任何审批失败或安全拒绝都产生对应 `tool_result`，不能跳过或错误复用另一调用的授权。
- 不允许模型或 MCP 注解绕过静态 Deny、沙箱和敏感路径保护。

### 6.4 工具结果预算与外置保存

目前 `current_result_budget` 会将同一个可变 `ResultBudget` 对象继承到子 Task。如果多个工具完成时各自调用 `bound()`，共享 `remaining` 和外置存储配额可能出现竞争。

建议改造为：

1. 工具执行阶段只产出原始受控结果对象，不写 `ExecutionContext.messages`。
2. Collector 在结果收齐后，按调用顺序统一分配轮次 Token/字节预算。
3. 需要外置的结果交 `ToolResultStore` 持久化并返回安全摘要引用。
4. 结果存储配额检查及文件创建使用同一服务内原子保护。
5. 记录 `output_storage_error` 时不盲目重放可能已成功的副作用操作。

现有 `invoke_tool()` 的 `current_result_budget` 处理需要同步调整，不能仅在 Scheduler 外再做一遍截断，导致重复扣减。

### 6.5 ExecutionContext 与 LLM 工具协议

- `AgentLoop` 先写入包含本轮所有 `tool_use` 的 assistant 消息。
- Scheduler 返回与输入等长、按输入顺序排列的结果列表。
- `AgentLoop` 在**同一处串行写入**所有 `tool_result`；子 Task 不修改父上下文。
- 一个 `tool_use_id` 必须对应且仅对应一个结果；拒绝、超时、失败、预算耗尽都需要匹配的错误结果。
- 完成本轮结果提交与检查点持久化后才能进入下一次 LLM 请求。
- 维持工具结果的不可信数据边界，不允许工具输出提升指令优先级。

## 7. 代码改造范围

### 新增文件

- `src/claudedock/core/tools/scheduler.py`：批次执行、任务管理与顺序归并。
- `src/claudedock/core/tools/concurrency.py`：并发元数据、资源声明、冲突判断；P1 再引入 DAG。
- `tests/unit/test_tool_scheduler.py`：调度正确性、结果配对、异常/取消。
- `tests/integration/test_parallel_tool_execution.py`：与权限、Journal、上下文消息及故障恢复联动。
- `tests/benchmarks/test_parallel_tools.py`：可重复的串行/并发延迟基准。

### 修改文件

- `src/claudedock/core/loop.py`：将串行 `for + await` 替换为 Scheduler 批次调用，保留预算和停滞检测。
- `src/claudedock/core/tools/base.py`、`tools/registry.py`：可信并发元数据与注册校验。
- `src/claudedock/core/tools/invocation.py`：拆清审批等待、执行与结果后处理，保留安全入口。
- `src/claudedock/core/journal.py`：操作状态安全更新与恢复测试。
- `src/claudedock/core/compact/tool_result_store.py`：集中预算、并发配额保护。
- `src/claudedock/core/permissions/manager.py`：审批队列和作用域一致性。
- `src/claudedock/core/context.py`：必要时增加批量结果写入 API，确保只由 AgentLoop 更新。
- `src/claudedock/core/config.py`、`core/runner.py`：配置注入、默认关闭及灰度策略。
- `src/claudedock/core/bus/events.py`、`monitoring/metrics.py`：批次执行事件与指标。

## 8. 分阶段实施计划

### P0：安全只读并发 MVP

- 实现 Scheduler、调用 ID 校验、连续只读工具分组及非只读串行屏障。
- 使用 TaskGroup、每 Run 与全局双层限流；保留 `invoke_tool()`。
- 收集后统一按模型调用顺序提交 `tool_result`。
- 评估并改造阻塞文件 I/O；测试真正的执行重叠。
- 验收：串行模式全部回归通过，独立只读工具确实并发，写操作无顺序退化。

### P1：资源级冲突调度

- 引入 `ResourceClaim`、路径规范化、冲突关系与 DAG 调度。
- 非重叠、可信、可证明安全的资源操作可并发；不确定者默认串行。
- 对动态工具、MCP、Bash 保持保守策略。
- 验收：读写冲突、写写冲突、路径别名与目录交叉覆盖测试通过。

### P2：共享状态与可靠性

- 完成 Journal 状态保护、审批/执行队列分离、Collector 集中结果预算、配额原子治理。
- 实现取消传播、未决状态处理、断点恢复及故障注入测试。
- 验收：无结果丢失、错配、重复副作用盲目重试和审批串用；超时后资源可回收。

### P3：可观测性、评测和灰度启用

- 记录批次生命周期、队列等待、工具并发峰值、冲突降级和取消事件。
- Shadow 阶段只记录“理论可并发”决策，不改变执行；随后按安全工具类别开启灰度。
- 对比串行基线：端到端 P50/P95、批次耗时、吞吐量、错误率、Token/结果存储消耗。
- 验收：安全回归无退化，有可复现实验数据后再决定扩大并发范围。

## 9. 推荐配置（新增字段，尚未在代码中实现）

```toml
[tool_execution]
parallel_enabled = false          # 默认安全关闭
policy = "safe_only"              # safe_only / resource_scoped
max_concurrency_per_run = 4
max_calls_per_batch = 8
max_pending_approvals = 8
```

参数应由 `ClaudeDockConfig` 校验后经 `AgentRunner` 注入 Scheduler。全局上限应与项目已有共享 `tool_slots` 统一治理，避免多重无序 Semaphore 造成饥饿。

## 10. 测试与验收清单

### 正确性

- [ ] 单工具及串行开关行为与旧版本一致。
- [ ] 多个独立只读工具实际发生执行时间重叠。
- [ ] 完成顺序颠倒时结果仍按原顺序绑定正确 `tool_use_id`。
- [ ] 同轮 ID 重复或缺失时拒绝执行并给出明确错误。
- [ ] 同一资源的读写、写写冲突保持顺序。
- [ ] 所有工具调用（包含错误与拒绝）均有且仅有一个结果。
- [ ] `ExecutionContext` 消息结构与 Claude 工具协议保持兼容。

### 安全与可靠性

- [ ] 每个 Tool Call 均经过现有 SecurityGate/PermissionManager。
- [ ] 审批不占用实际执行槽，超时或取消不遗留待审批 Future。
- [ ] 重试仅遵守工具安全声明，不因批次失败重放有副作用操作。
- [ ] Journal 状态及操作 ID 不冲突，未决结果优先核查而非盲目重放。
- [ ] 父 Run 取消时子工具任务释放，外部超时状态准确记录。
- [ ] ResultBudget 与 ToolResultStore 并发时总配额不超限。
- [ ] 事件日志/Trace 不泄露敏感参数。

### 性能评测

- [ ] 使用可控模拟延迟的异步工具，对比串行/并发批次耗时。
- [ ] 加入真实文件读取、检索和网络工具，验证真实性能收益。
- [ ] 记录 P50/P95 延迟、并发峰值、队列等待、限流和失败率。
- [ ] 压力测试多个 Run、多个子 Agent 与工具批次共享资源时的稳定性。
- [ ] 不将模拟耗时或理想加速比宣传为真实生产指标。

## 11. 可观测性指标

```text
tool_batch_duration_ms
tool_batch_size
tool_batch_parallelism
tool_queue_wait_ms
tool_conflict_serialized_total
tool_batch_failed_total
tool_batch_cancelled_total
tool_result_mismatch_total
```

建议新增 `tool.batch_planned`、`tool.batch_started`、`tool.batch_finished` 事件，携带 `run_id`、`step`、`batch_id`、调用数、串行原因与耗时；不记录未脱敏工具参数。Trace 关联现有 `tool.invoke`，以时间线展示重叠执行与冲突屏障。

## 12. 关键工程决策与总结

1. **Agent 级并发与工具级并发分离**：后台子 Agent 沿用 Supervisor/Registry/agent_result；单轮工具并行采用 TaskGroup + Result Collector。
2. **可信调度决策**：LLM 负责提出调用，宿主负责按工具能力、资源访问与权限实际决定并发。
3. **默认串行、逐步放开**：先安全只读批次，再资源级冲突 DAG，未知工具和高风险操作保守处理。
4. **执行安全优先于吞吐**：SecurityGate、审批、幂等、Journal、审计和结果预算必须保持有效。
5. **结构化并发与确定性提交**：TaskGroup 管理任务收敛，按原始调用顺序返回模型。
6. **性能结论以实验为准**：不把“创建了多个 Task”当成实际 I/O 提速，更不臆造业务性能数据。
7. **不引入不必要的分布式组件**：当前单进程守护架构下，Asyncio 已足够实现安全、有界并发。

## 13. 参考资料

- Python 3.12 asyncio TaskGroup：https://docs.python.org/3.12/library/asyncio-task.html#task-groups
- Anthropic Tool Use：https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/overview
- LangGraph ToolNode：https://langchain-ai.github.io/langgraph/reference/agents/
- MCP Tools：https://modelcontextprotocol.io/specification/2025-06-18/server/tools

---

**说明**：本文保留升级设计和原始验收目标。实际实现、测试覆盖、本地基准和仍需进行的生产灰度验证，以 [实施记录](parallel-tool-execution-implementation.md) 为准；不能将设计目标视为生产性能结论。
