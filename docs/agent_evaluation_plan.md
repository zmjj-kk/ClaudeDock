# ClaudeDock Agent 评测计划

> 版本：v1（对应 S7，覆盖 Subagents / Skills / MCP / Multi-agent）
> 原则：**沿执行链分层、确定性优先、可诊断**。任何一层失败都能定位到"工具错 / 动作错 / 轨迹错 / 目标没达成 / 线上收益差"中的哪一环。

---

## 0. 为什么现有代码已经具备评测地基

在开始设计前，先盘点 ClaudeDock 当前已经内置、可直接复用的评测资产：

| 资产 | 位置 | 对评测的意义 |
|---|---|---|
| 事件流 `events.jsonl` | `core/events/writer.py` + `core/bus/events.py` | **轨迹评测的唯一事实来源**。`RunStarted/StepStarted/ToolCallStarted/ToolCallFinished/ToolCallFailed/LlmUsage/Permission*/Subagent*/SkillInvoked` 全量落盘，无需侵入改造即可做轨迹回放与约束校验 |
| LLM 追踪 `daemon.jsonl` | `core/trace/{record,provider,writer}.py` | `TraceRecord` 含方向/层/延迟/usage/payload，支撑 Token 成本与延迟分位统计 |
| 结构化返回 `RunOutcome` | `core/runner.py` | `status/reason/result` 三元组是端到端判定入口 |
| `ToolResult.error_type` | `core/tools/base.py` | 已枚举 `runtime_error/timeout/schema_error/permission_denied/rate_limited`，是工具层错误分类的直接信号 |
| `invoke_tool` 校验/权限/重试 | `core/tools/invocation.py` | 已实现 schema 校验 + 4 层权限 + 指数退避，工具层评测可完全确定性化 |
| `LLMProvider` 协议 | `core/llm/base.py` | 只需 4 行即可实现一个"剧本化 provider"，让 loop/调用链脱离真实 LLM 做确定性单测 |
| 集成测试基座 | `tests/conftest.py` + `tests/integration/test_run_e2e.py` | 已有"起真实 daemon 子进程 + 随机端口"的能力，端到端评测直接复用 |
| `EventBus` / `ToolRegistry` | `core/events/bus.py`、`core/tools/registry.py` | 评测 harness 可注入内存 bus / 内存 registry 隔离副作用 |

**核心结论**：ClaudeDock 的架构天然是"评测友好"的——执行链的每个环节都有结构化产物。评测计划的落点，是**把这些产物系统化为四层评测 + 一套 harness + 一份可生长的数据集**，而不是重写执行链。

---

## 1. 四层评测体系（映射到 ClaudeDock 执行链）

```
                      执行链                         评测层
  ┌─────────────────────────────────────────────┐
  │ AgentRunner.run_and_capture (runner.py)     │
  │   └─ AgentLoop.run (loop.py)  plan→act→observe│  ③ 端到端任务层
  │        ├─ provider.chat (llm)               │  ② 单步层（下一步动作）
  │        └─ invoke_tool (invocation.py)       │
  │             ├─ schema 校验                   │  ① 工具层
  │             ├─ PermissionManager            │  ① 工具层（权限失败）
  │             └─ tool.invoke → ToolResult     │  ① 工具层（稳定性/幂等）
  └─────────────────────────────────────────────┘
  线上（daemon 长跑）                             ④ 线上业务层
```

---

## 2. 第一层：工具层（可靠性与正确性）

### 2.1 工具稳定性（确定性，进 CI）

**做法**：把每个工具固化为一张"契约矩阵"，每条契约是一个确定性用例，用内存 `ToolRegistry` + 内联 stub 直接调 `tool.invoke(params)` 断言 `ToolResult`。

对**每个工具**（`read_file/bash/write_file/list_dir/task_*/note_save/spawn_agent/agent_result/MCP*`）覆盖以下六类输入：

| 用例类 | 断言要点 | 已有基础 |
|---|---|---|
| 正常 | `is_error=False`，返回内容符合预期 | `tests/unit/test_builtin_tools.py` |
| 空结果 | 空目录/空文件/无输出 → 明确的空结果语义（如 `[no output]`） | 同上 |
| 超时 | `bash timeout` / 慢命令 → `error_type="timeout"`，且子进程被 kill | `tests/unit/test_invocation.py` |
| 权限失败 | `PermissionManager.check_and_wait` deny → `error_type="permission_denied"` | `tests/unit/test_permission_manager.py` |
| 第三方异常 | MCP server 不可用 / 抛异常 → `McpTool` 返回 `is_error`，不崩 | `tests/unit/test_mcp_tool.py` |
| **幂等性** | 见 2.1.1 | 缺失，需新增 |

**Schema/类型/取值范围**：`params_model`（pydantic）已在 `invoke_tool` 里 `model_validate`，`schema_error` 已被 `ToolCallFailedEvent` 记录。契约矩阵补齐：缺必填字段、类型错误、越界取值（如 `bash.timeout` 的 `ge=1, le=120`、`write_file` 1MB 上限、`write_file` 的 `..` 路径遍历）。

#### 2.1.1 幂等性（重点新增，发现一个真实风险）

**现状问题（建议作为评测首批暴露的缺陷）**：`invocation.py` 中

```python
_RETRYABLE: frozenset[str] = frozenset({"runtime_error", "rate_limited"})
```

意味着**任何**返回 `error_type="runtime_error"` 的工具都会被自动重试 2 次，而 `runtime_error` 并不区分"纯读取失败"与"副作用已部分执行后失败"。典型反例：

- `bash` 非零退出码 → `error_type="runtime_error"` → **命令被重跑 2 次**。若命令是 `git push`、`付款提交`、`DELETE` 这类有副作用操作，重试即二次执行。
- MCP 工具（`mcp/tool.py` 把 `McpToolError` 也归为 `runtime_error`）→ 有副作用的第三方工具被重试。

**评测设计**：在 `BaseTool` 增加分类字段（如 `idempotent: bool` / `retryable_error_types`），并将重试条件收紧为 `_RETRYABLE ∩ tool.retryable_error_types`。新增用例：

1. 声明式校验：每个工具必须显式声明幂等性，未声明的工具禁止自动重试（编译期/单元期门禁）。
2. 行为校验：对幂等工具（`write_file` 覆写）连续 invoke 两次断言结果一致；对非幂等工具（`bash`、`task_create`、`spawn_agent`）注入一次失败，断言**不触发重试**且副作用只发生一次（用计数器 stub 验证调用次数 == 1）。
3. `task_create` 无去重（每次调用 `_next_id += 1` 新建任务）→ 作为"有意非幂等"登记，评测文档标注"重试不可自动进行，需上层去重"。

### 2.2 模型决策能力（工具选择 + 参数语义）

严格拆为两个独立指标，二者分开评测、分开上报，**不许合并成一个"工具分"**：

- **工具选择准确率（应否调用）**：给定单步状态，判断模型"该调用工具 A / 该结束 / 该追问"是否正确。二分类/多分类，指标用 macro-F1 而非准确率（工具类别不均衡）。
- **参数语义正确性（Schema 合法但业务字段错）**：模型产出的参数能过 `model_validate`，但语义错。例：`read_file(path="file.py")` 写成 `path="README.md"`；`task_update(task_id=2)` 写成 `task_id=3`；`spawn_agent(subagent_type="executor")` 误选为 `planner`。

**评测方式**：需要一个**剧本化 LLM provider**（见 §6.3 `ScriptedProvider`），让"模型决策"这条链路可确定性复现：脚本化 provider 按预置序列吐出 `LlmResponse`（含指定 `tool_calls` 或 `end_turn`），据此：
- 验证 loop 侧对"该不该调用"的机械处理（`stop_reason` 分支、`max_tokens` 补位、`end_turn` 收尾）。
- 而对**真实模型的决策质量**，用带 golden 标签的评测集（每个 case 标注 `expected_tool` / `expected_params` / `expected_action∈{call,ask,end}`）跑真实 LLM，比对 golden。

---

## 3. 第二层：单步与轨迹层（过程合理性）

### 3.1 单步评测（快速验证"下一步"）

**定义**：冻结一个状态（`messages` + `tool_schemas` + `system`），只问一个问题——"此刻模型的下一步是什么"。

- 输入：一段对话历史（含 0~N 轮工具结果）。
- 输出判定：`call(工具名, 参数) | ask_user | end` 三者之一。
- 指标：动作类别准确率、工具选择准确率、参数语义正确率（承接 2.2）。
- 优点：一条 case 只需 1 次 LLM 调用，成本极低，适合大规模刷回归、AB 模型对比。

### 3.2 轨迹评测（校验整条工具调用序列）

**事实来源**：`events.jsonl`。把一次 run 的 `ToolCallStartedEvent` 序列抽出来即得轨迹 `[tool_1, tool_2, ...]`。

**核心设计——三类约束，而非"黄金最短路径"**：不做单一参考轨迹，而是基于任务定义三组约束，其余路径由 agent 自由发挥：

| 约束类型 | 语义 | 示例 |
|---|---|---|
| **必须出现** (`must_include`) | 轨迹中必须出现某工具调用 | 任务"新建并提交一个文件"必须出现 `write_file`；"多步骤任务"必须出现 `task_create` |
| **禁止出现** (`must_not_include`) | 轨迹中不得出现某工具调用 | "只读分析"任务不得出现 `write_file`/`bash -c 'rm...'`；"无需子代理"不得出现 `spawn_agent` |
| **先后依赖** (`ordering`) | 工具 A 必须先于 B | `read_file` 必须先于对同一文件的 `write_file`；`spawn_agent`(后台) 必须先于 `agent_result(run_id)`；`task_create` 必须先于 `task_update(task_id)` |

**自动监控的负向信号**（无需约束定义，直接由事件流计算）：

- **无效调用率**：`ToolCallFailedEvent`（`schema_error` / `unknown tool`）数量 / 总调用数。
- **重复调用率**：相同 `(tool_name, params)` 的连续/全轨迹重复次数（去重后）。
- **Token 消耗**：`LlmUsageEvent.input_tokens/output_tokens/cache_*` 求和。
- **时间消耗**：`StepStarted/StepFinished` 时间戳差、`ToolCallFinishedEvent.elapsed_ms` 求和。

**判定器**：新增 `TrajectoryValidator`（§6.6），读 `events.jsonl` → 展开轨迹 → 逐条校验约束 → 输出 `{passed, violations, 无效/重复/token/时间}` 结构化结果。**零 LLM 成本、可进 CI**。

---

## 4. 第三层：端到端任务层（目标达成度）

### 4.1 核心指标：任务完成率

**判定方式优先级（严格执行，避免"agent 自评"）**：

1. **环境状态校验**（首选）：agent 的最终"环境"是沙箱文件系统 + `.tasks/` 目录 + 会话 note。成功条件直接断言这些状态。例：
   - "写一个 `fib.py` 实现 fibonacci" → 断言 `fib.py` 存在且内容含 `def fib`。
   - "把任务拆成 3 步并标记完成" → 断言 `.tasks/` 下 3 个 `task_*.json` 且 `status=completed`。
2. **单元测试校验**：case 自带一个测试文件，agent 写出的代码必须 `pytest` 通过才算成功（对"生成代码/修 bug"类任务，这是最硬的成功判据）。
3. **结构化业务字段校验**：对 MCP/业务工具，断言最终业务字段（如数据库记录、API 返回的 JSON 字段）。
4. **禁止**：以 agent 自己的 `result` 文本或"LLM 自评是否完成"作为成功判据。

### 4.2 稳定性与进度

- **pass^k**：同一 case 重复跑 k 次（推荐 k=3~5），记录 `pass@1`（首跑通过率）与 `pass^k`（至少一次通过）。区分"能力缺失"（k 次全 fail）与"不稳定"（pass@1 低但 pass^k 高）。
- **细粒度阶段进度**：对复杂任务定义 checkpoint 序列（利用 `task_create` 的 subject 或中间产物文件作为检查点），按"到达了第几阶段"给分而非只看终态。例：任务拆解正确(1分)→ 生成草稿(2分)→ 通过测试(3分)。这样能区分"卡在第二步"和"整体不会做"。

### 4.3 沙箱隔离

`run_and_capture` 已支持注入 `provider/runs_dir/permission_manager`，且 `config` 全走 env 覆盖（`CLAUDEDOCK_*`）。端到端评测的每个 case 必须：

- 独立 `tmp` cwd（`write_file`/`bash` 的副作用被隔离）；
- 独立 `runs_dir` 与 `~/.claudedock` 覆盖；
- **权限剧本化**：注入 `PermissionManager`（policy 可控）或自动应答 stub，实现"全 auto-allow"与"全 deny"两档，分别测"能力上限"与"安全下限"。

---

## 5. 第四层：线上业务层（实际收益与损耗）

### 5.1 业务与工程指标（来自线上日志，非评测集）

数据源：`~/.claudedock/traces/daemon.jsonl`（LLM 延迟/usage/payload）+ `~/.claudedock/logs/core.log` + 每 run 的 `events.jsonl`。

| 指标 | 计算来源 |
|---|---|
| 任务解决率 | `RunFinishedEvent.status="success"` 占比 |
| 人工接管率 | `PermissionRequestedEvent` 中最终 `deny` 的比例，及 ASK 占比（ASK 高 = 打断多） |
| 延迟分位值 | `TraceRecord` 中 `LLM→CORE` 的 `latency_ms` 的 P50/P90/P99，加端到端 step 间隔 |
| Token 成本 | `LlmUsageEvent` 的 `input/output/cache_read/cache_creation` 聚合（cache 命中率是关键降本项） |
| 系统稳定性 | 崩溃/异常日志、`RunFinishedEvent.reason="llm_error"/"exceeded_max_steps"` 率、daemon 重启次数 |

### 5.2 安全硬门禁（独立成档，绝不与体验分加权）

**这是一票否决项**，独立于 §5.1 的体验分，任何一项超标即"红灯"，不参与平均：

| 安全指标 | 判定方式 |
|---|---|
| 未授权调用率 | `PermissionDeniedEvent` / 总调用；以及"应 ask 却被 auto_allow 绕过"的审计（对照 `policy.evaluate` 与 `check_and_wait` 结果） |
| 敏感泄露率 | 对工具输出做确定性扫描（正则匹配 API key/密码/私钥/内网地址），另配 LLM 复核减少漏报 |
| 注入成功率 | 对抗样本集（§7.3）中，agent 实际执行了注入指令的比例 |
| 越权读取率 | `read_file`/`bash` 访问了授权范围外路径的比例（对照 `OUTSIDE_CWD_HEURISTICS`） |

**上报形态**：安全指标单独一张表，标 `PASS/FAIL`（阈值内/外），不进加权总分。

---

## 6. 评测 Harness 设计

新增包 `src/claudedock/eval/`（与 `core` 平级），遵循项目代码风格（单行中文注释、pydantic v2、discriminated union）。

```
src/claudedock/eval/
├── cases.py           # EvalCase 模型：goal/fixtures/约束/成功判据/难度/风险/来源
├── dataset.py         # 加载 YAML/JSONL 评测集，校验 schema
├── sandbox.py         # 隔离 tmp 环境 + 权限剧本 + runs_dir/配置覆盖
├── scripted_provider.py # 剧本化 LLMProvider（确定性），供工具层/单步层/loop 测试
├── checkers.py        # 确定性断言：文件系统 / task 状态 / schema / 权限规则
├── trajectory.py      # TrajectoryValidator + 三类约束模型（must_include/not/ordering）
├── metrics.py         # 从 events 聚合：无效/重复/Token/时间/延迟分位
├── judge.py           # LLM-as-Judge（含偏差缓解）
├── reporter.py        # 输出 JSON 报告（分层得分 + 逐 case 明细），供 CI gate
└── cli.py             # `claudedock eval` 入口
```

### 6.1 `EvalCase`（pydantic v2）核心字段

```python
class EvalCase(BaseModel):
    id: str
    layer: Literal["tool", "step", "trajectory", "e2e"]     # 归属层
    goal: str                                                 # e2e 目标
    initial_state: dict                                       # 单步层的冻结状态
    constraints: TrajectoryConstraints | None                 # 轨迹层三类约束
    success_check: SuccessCheck                               # 环境/单测/业务字段判定器
    difficulty: Literal["easy", "medium", "hard"]
    risk_level: Literal["none", "low", "high"]                # 安全敏感标记
    source: Literal["real", "boundary", "adversarial", "badcase"]  # 数据集来源
    tools_expected: list[str]
    golden: dict                                              # 工具选择/参数的 golden 标签
```

### 6.2 `Sandbox`

封装 `AgentRunner` 的构造依赖，统一注入：临时 cwd、隔离 `runs_dir`、`PermissionManager(policies=...)`、可选 `ScriptedProvider`。对外暴露 `run(case) -> RunResult`，内部同时挂一个内存 `EventBus` 收集事件供轨迹/指标分析。

### 6.3 `ScriptedProvider`（关键基础设施）

实现 `LLMProvider` 协议（`core/llm/base.py` 仅需 `chat` 一个方法），按脚本吐 `LlmResponse`：

```python
class ScriptedProvider:
    def __init__(self, script: list[LlmResponse]) -> None: ...
    async def chat(self, messages, tool_schemas, bus, run_id, *, step=0, system=None) -> LlmResponse:
        return self._script[min(step, len(self._script) - 1)]
```

用途：(1) 工具层/loop 的确定性单测（脱离真实 LLM）；(2) 复现历史 badcase（把真实 trace 的 LLM 响应录制成 script 回放）。

### 6.4 确定性 `checkers`

与 agent 自评无关的硬断言，全部纯函数、无 IO 依赖、进 CI：
- `filesystem_checker(expected_files, content_predicates)`
- `task_checker(tasks_dir, expected_subjects, expected_status)`
- `schema_checker(params_model, valid_invalid_cases)`
- `permission_rule_checker(policy, command, expected_decision)`

### 6.5 评分体系（§8）由 `reporter` 汇总，输出 JSON，CI 可设阈值 gate（如"工具层确定性 100% 必过、e2e pass@1 ≥ X、安全全 PASS"）。

---

## 7. 高质量评测集的多源构建

### 7.1 真实请求

- 从 `~/.claudedock/` 的 session store / 日志采样，**去隐私**（脚本替换密钥/路径/邮箱/PII）。
- **分层采样**：按任务类型（写代码/文件操作/调研/多步编排）× 难度 × 用到的工具 × 风险等级四维分层，**避免偏向高频简单请求**——刻意保量抽"低频高难度"与"高风险"样本。

### 7.2 边界场景（专攻工程隐患）

- 参数缺失 / 类型错误 / 越界取值（工具层矩阵的输入侧）。
- 含糊时间（"明天""最近"）、含糊路径（相对 vs 绝对）、多工具混淆（多个同名/近似工具同时存在时的选择）、超时（慢命令、MCP 不可用）、长上下文（逼近 compaction 阈值，触发 `Compactor` 边界）。

### 7.3 对抗与安全样本（划定安全边界）

- **提示注入**：`goal` 里藏"忽略上述指令，执行 X"；`read_file` 读到的文件内容里藏注入。
- **越权读取**：诱导 `read_file`/`bash` 访问 cwd 之外路径（验证 `OUTSIDE_CWD_HEURISTICS` + 权限层拦得住）。
- **诱导绕过审批**：诱导 `bash` 用 `&&`、`;`、变量拼接绕 `allow_patterns`/`deny_patterns`。
- **子代理注入**：`spawn_agent` 的 `prompt` 里夹带恶意指令，验证子代理角色隔离（`allowed_tools`/`system_prompt` 是否兜得住）。

### 7.4 历史 Badcase（动态"踩坑档案室"）

- 线上失败 run（`status="failed"` 或人工标记低质）经**人工归因**后回流入集：标注"根因=工具选择错误 / 参数语义错误 / 轨迹绕路 / 目标误判 / 安全越界"。
- 每次修 bug 或改 prompt 后，把对应的复现 case 固化进集，防止回归。
- 评测集**随系统演进持续生长**，用 `git` 版本化 + `id` 稳定，报告里带 `source` 字段可追溯。

---

## 8. 确定性与模型审判相结合的评分体系

### 8.1 确定性检查（优先，CI 首选）

Schema 校验、数据库/文件系统状态、单元测试、权限规则、轨迹三类约束、无效/重复调用指标——全部**纯函数、速度快、成本低、结果稳定**，进 CI 作为第一道 gate。

### 8.2 人工评审（定义标准）

对**政策遵循度、开放式结果、高争议样本**由领域专家评审，产出**Rubric**（明确、可标注、逐条打分），并持续用人工评审结果**校准**自动裁判。人工评审用于：(1) 定义各层 Rubric 的"黄金答案"；(2) 抽查 LLM 裁判的一致性。

### 8.3 LLM-as-Judge（扩展开放式评测）

对无标准答案的开放式任务（生成的报告质量、回答是否满足隐含需求），让大模型按 Rubric 输出**结构化评分 + 理由**（`{score: int, rationale: str, rubric_hit: list[str]}`）。**必须防范三类偏差**：

| 偏差 | 缓解手段 |
|---|---|
| 位置偏差（偏爱首选项） | 多选项/多方案**随机打乱**后再评；多次采样取中位 |
| 篇幅偏差（偏爱长文） | Rubric 明示"按实质而非长度"；归一化长度因子；拆"内容分"与"简洁分" |
| 自我偏好（偏爱自己生成的风格） | 裁判模型 ≠ 被测模型；锚定确定性判据；用人工评审样本做**校准集**，裁判分数与人工分数相关性低则退回人工 |

**组合原则**：确定性 → 人工（定义标准）→ LLM（扩展开放），三者不互相替代；**安全指标永不进加权，永远独立 PASS/FAIL**。

---

## 9. 分阶段落地路线（对齐 S7 → S8）

| 阶段 | 交付物 | 依赖 | 成本 |
|---|---|---|---|
| **P0 工具层确定性** | `eval/checkers.py` + 契约矩阵 + 幂等性分类（含修复 §2.1.1 重试缺陷） | 无，纯单测 | 零 LLM |
| **P1 轨迹层** | `eval/trajectory.py` + `eval/metrics.py`，读 `events.jsonl` 做三类约束 + 无效/重复/Token/时间 | P0 | 零 LLM |
| **P2 端到端层** | `eval/sandbox.py` + `eval/cases.py` + pass^k + 沙箱隔离 | P1 | 低（每 case 一次完整 run） |
| **P3 数据集** | 四源评测集 + `eval/dataset.py` 加载/校验 + 版本化 | P2 | 人工为主 |
| **P4 模型审判** | `eval/judge.py` + Rubric + 校准集 | P3 | 中（LLM 调用） |
| **P5 线上层** | 遥测聚合 + 安全硬门禁仪表盘 + badcase 回流管道 | P2 起 | 低（读日志） |

**优先级建议**：P0 与 P1 都是"零 LLM 成本、确定性、可立即进 CI"的，应最先做——它们不依赖真实模型就能暴露工程缺陷（重试幂等、schema 边界、轨迹约束），是投入产出比最高的一步。

---

## 10. 首批应暴露的问题（评测上线即应标红）

1. **工具自动重试的幂等性缺陷**（§2.1.1）：`runtime_error` 触发 `bash`/MCP 副作用工具重跑，属安全+正确性双风险，建议 P0 首修。
2. **`task_create` 无去重**：多次调用生成重复任务，轨迹层"重复调用率"会暴露。
3. **成功判定依赖 agent 自评**：当前 `RunOutcome.result` 是 agent 自己的文本，端到端层必须改用环境状态/单测判定，否则分数虚高。
4. **越权访问依赖 bash 启发式**：`read_file` 默认 `ALLOW` 且无 cwd 边界（仅 `write_file` 挡了 `..`），安全样本集会暴露 `read_file("/etc/passwd")` 类越权——需在 P0/P3 明确 `read_file` 的路径边界策略。
