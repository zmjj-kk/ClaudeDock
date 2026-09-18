# KamaClaude Agent 评测报告（S7 · 2026-09-17）

> 评分依据：`docs/evaluation/rubric.md`
> 评测方式：代码静态审阅 + 单元测试运行 + 评测体系落地现状盘点（本次评测基于证据，非 agent 自评）。

---

## 0. 总览

| 层 | 主题 | 得分 | 评级 |
|---|---|---:|---|
| 第一层 | 工具层（可靠性与正确性） | **74 / 100** | 良 |
| 第二层 | 单步与轨迹层（过程合理性） | **40 / 100** | 待建设 |
| 第三层 | 端到端任务层（目标达成度） | **30 / 100** | 待建设 |
| 第四层 | 线上业务层（实际收益与损耗） | **30 / 100** | 待建设 |

**总体成熟度（加权）** = 74×30% + 40×25% + 30×25% + 30×20% = **46 / 100**（早期阶段）。

**结论**：执行链的地基（工具、事件流、遥测）已打牢，第一层工具层达到"良"；但**评测体系本身（P1–P5）尚未建成**，第二、三、四层的低分主要反映"可诊断性缺失"，而非"系统不能用"。当前最大的可诊断性短板是：**成功判定依赖 agent 自评、轨迹无校验器、线上无安全门禁**。

---

## 1. 第一层：工具层 — 74/100（良）

| 子项 | 权重 | 得分 | 证据与说明 |
|---|---|---:|---|
| Schema 与类型校验覆盖 | 30 | 18 | 11 个内置/子代理工具中 7 个有 `params_model`（bash/write_file/read_file/list_dir/note_save/spawn_agent/agent_result）；**task_create/update/get/list 四个缺失**，缺参走 `KeyError→runtime_error` 而非 `schema_error` |
| 错误分类与"不抛异常"契约 | 25 | 23 | `error_type` 五类齐全（runtime_error/timeout/schema_error/permission_denied/rate_limited），`invoke_tool` 兜底不抛；扣 2 分因 task_* 的 KeyError 会漏成 runtime_error |
| 幂等性与重试安全 | 25 | 22 | **P0 刚修复**：`retryable_errors` 分类正确（只读可重试 / 副作用不重试 / rate_limited 恒可重试），回归测试 4 条锁定；扣 3 分因 `task_create` 无去重仍是非幂等隐患未登记 |
| 权限与安全边界 | 20 | 11 | `bash` 有 `deny_patterns` + `OUTSIDE_CWD_HEURISTICS`（绝对路径/`~`/`..` 强制 ASK）；但 **`read_file`/`list_dir` 默认 ALLOW 且仅挡 `..`，不挡绝对路径**，`read_file("/etc/passwd")` 可成功 |

**工具层改进意见（按优先级）**：

1. **[高/安全]** 给 `read_file`/`list_dir` 增加与 `bash` 同级的路径边界（绝对路径/`~` 强制 ASK 或拒绝），否则越权读取是确定漏洞。
2. **[高]** 补 `task_create/update/get/list` 的 `params_model`，让缺参/错类型正确分类为 `schema_error`，与其它工具对齐。
3. **[中]** `task_create` 无去重 → 显式声明"非幂等、不可自动重试"，或加幂等键。
4. **[中]** 用 `eval/checkers.py` 补齐工具契约矩阵（空结果/超时/第三方异常/幂等双击用例），把散落的单测收敛为声明式契约。

---

## 2. 第二层：单步与轨迹层 — 40/100（待建设）

| 子项 | 权重 | 得分 | 证据与说明 |
|---|---|---:|---|
| 可观测性原料 | 40 | 34 | `events.jsonl` 已覆盖 Run/Step/ToolCall/LlmUsage/Permission/Subagent/Skill 全量，`ToolCallFailedEvent` 带 `attempt`；扣 6 分因工具结果正文未结构化落盘（仅 `output` 字段） |
| 轨迹校验器（三类约束） | 30 | 0 | 未实现 `TrajectoryValidator`；`tests/integration/test_run_e2e.py` 里是手写事件断言，无 must_include/forbidden/ordering 通用约束 |
| 负向指标聚合 | 30 | 6 | 无效/重复调用、Token、耗时原料在事件里可算，但无聚合器；`kama trace` 仅逐条展示 |

**轨迹层改进意见**：

1. **[高]** 实现 `eval/trajectory.py`：`TrajectoryValidator` + 三类约束模型（must_include / must_not_include / ordering），读 `events.jsonl` 输出 `{passed, violations}`。零 LLM 成本，可进 CI。
2. **[高]** 实现 `eval/metrics.py`：从事件聚合无效调用率（schema_error/unknown tool）、重复调用率（同 tool+params）、Token 消耗（`llm.usage`）、耗时（`elapsed_ms`/step 时间戳）。
3. **[中]** 实现 `eval/scripted_provider.py`：剧本化 `LLMProvider`，使 loop/单步决策脱离真实 LLM 做确定性复现（也是 badcase 回放的基础）。

---

## 3. 第三层：端到端任务层 — 30/100（待建设）

| 子项 | 权重 | 得分 | 证据与说明 |
|---|---|---:|---|
| 端到端执行能力 | 30 | 22 | `AgentRunner.run_and_capture` + `test_run_e2e.py` 可走通真实链路；扣 8 分因依赖真实 `ANTHROPIC_API_KEY`（缺失即 skip），无离线确定性 harness |
| 确定性成功判定 | 30 | 8 | 仅 1 个 smoke test 断言 `status=="success"`（**agent 自报**）；无环境状态 / 单测 / 业务字段 checker，这是分数虚高的主要风险 |
| 稳定性 pass^k + 阶段进度 | 20 | 0 | 未实现 |
| 沙箱隔离 | 20 | 0 | 无专门 harness，仅测试内 `tmp_path` + `monkeypatch.chdir` |

**端到端层改进意见**：

1. **[高]** 实现 `eval/checkers.py` 的成功判定器：`filesystem_checker` / `task_checker`（读 `.tasks/`）/ `unittest_checker`（agent 写出的代码须 pytest 通过），**禁用 agent 自评**。
2. **[高]** 实现 `eval/sandbox.py`：独立 cwd / runs_dir / 配置 / 权限剧本（auto-allow 与 deny 两档），支持并行。
3. **[中]** 实现 pass^k（k=3~5）与 checkpoint 阶段给分，区分"能力缺失"与"不稳定"。
4. **[中]** 构建首批评测集（先 20~30 条：文件操作 / 多步任务编排 / 生成代码过单测三类）。

---

## 4. 第四层：线上业务层 — 30/100（待建设）

| 子项 | 权重 | 得分 | 证据与说明 |
|---|---|---:|---|
| 遥测采集 | 35 | 30 | trace/events/logs 三路采集 + `kama trace` 查看器（含 latency/usage 摘要）；扣 5 分因 `trace.include_llm_payload` 默认 true，完整 LLM payload 落盘**未去敏**，有隐私风险 |
| 业务指标聚合 | 35 | 0 | 无任务解决率 / 人工接管率 / 延迟 P50/P90/P99 / Token 成本 / 稳定性聚合 |
| 安全硬门禁 ★ | 30 | 0 / **FAIL** | 无未授权调用率 / 敏感泄露率 / 注入成功率 / 越权读取率监控；`read_file` 越权（见 §1）叠加无监控 = 安全红灯 |

**线上层改进意见**：

1. **[高/安全]** `trace.include_llm_payload` 默认改为 false 或加去敏层，避免敏感数据进入 `daemon.jsonl`。
2. **[高]** 实现遥测聚合器：从 `RunFinishedEvent` 算解决率、从 `Permission*` 事件算接管率、从 `TraceRecord.latency_ms` 算分位、从 `LlmUsageEvent` 算 Token 成本（含 cache 命中率）。
3. **[高/安全]** 建安全硬门禁独立档（PASS/FAIL），四指标：未授权调用率、敏感泄露率、注入成功率、越权读取率；**不与体验分加权**。

---

## 5. 全局改进路线（对齐计划 P0–P5）

| 优先级 | 动作 | 层 | 成本 |
|---|---|---|---|
| **P0 收尾** | 补 task_* 的 `params_model`；`read_file`/`list_dir` 路径边界；`trace` payload 去敏 | 1/4 | 零 LLM |
| **P1** | `eval/trajectory.py` + `eval/metrics.py`（轨迹约束 + 负向指标） | 2 | 零 LLM |
| **P2** | `eval/sandbox.py` + 确定性成功判定 + pass^k | 3 | 低 |
| **P3** | 四源评测集（真实/边界/对抗/badcase）+ `eval/dataset.py` | 3 | 人工为主 |
| **P4** | `eval/judge.py`（LLM-as-Judge + 偏差缓解）+ Rubric 校准 | 开放题 | 中 |
| **P5** | 遥测聚合 + 安全硬门禁仪表盘 + badcase 回流 | 4 | 低 |

## 6. 附：本次评测附带发现的工程债（非四层范畴，但建议清理）

- `tests/unit/test_compactor.py` 用了 Python 3.12 已废弃的 `asyncio.get_event_loop()`，全量跑时 6 条因 loop 被前置 async 测试关闭而失败（单独跑通过）——建议改用 `asyncio.run` / pytest-asyncio。
- 全仓 48 个 ruff 告警（集中在 `test_memory_loader.py`、`test_permission_manager.py`、`test_compactor.py` 等未用 import）。
