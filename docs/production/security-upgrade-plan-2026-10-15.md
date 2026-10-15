# KamaClaude 安全机制生产级升级方案

> 日期：2026-10-15 | 性质：设计与实施计划（未修改运行代码）
> 参考：`C:\Users\Lenovo\Desktop\Claude_code  源码学习\安全机制\四层安全机制.doc`、`工具越权保护.doc`
> 范围：本仓库当前工作区（包含大量尚未提交修改）；实施前建立独立安全基线，不覆盖现有工作成果。

## 1. 设计结论

将文档中的“四层权限审查”改造为 **统一工具调用安全网关**：静态策略 → 工具特定硬约束 → 权限模式路由 → auto 模式 AI 风险分类；之后由隔离运行时和业务后端分别提供资源边界与业务鉴权。对于 MCP 工具，不能相信其自报的描述或风险等级；即使获得模型或用户授权，仍须经独立强制规则及后端权限检查。

原则：
1. 不可信外部资料、网页、仓库文件、MCP tool result、RAG、Skill、记忆只能作为数据，不能提升到 system/developer 指令或授予权限。
2. 强制禁止规则、文件系统边界、身份与业务审批不可被 auto、bypass、AI 分类器或缓存覆盖。
3. **权限批准 ≠ 沙箱隔离 ≠ 后端业务授权 ≠ Dry Run**，分别独立保障。
4. LLM 分类仅作辅助决策，错误、超时、解析失败及不确定时 fail-closed（拒绝或人工确认）。
5. 每次执行以规范化调用上下文为准：主体、session/run、工具真实身份、参数摘要、目标资源、风险与审批范围；最小权限、默认拒绝未知写操作。
6. 先保证无绕过、可观测与回归测试，再开启 auto；保留现有功能与兼容性。

## 2. 源码现状与差距（2026-10-15 工作区抽查）

| 当前代码 | 已确认的能力 | 需要解决的差距 |
|---|---|---|
| `core/permissions/manager.py` | deny regex、outside-cwd 强制 ask、session 参数 SHA256 授权缓存、审批事件、超时与连接归属检查 | 尚无显式模式引擎、工具自检统一接口、AI 动态分类；persistent deny 按工具名；缓存需要绑定用户主体/权限版本/工具服务端身份 |
| `core/permissions/policy.py` | allow/deny/ask、工具缺省策略、Bash 路径启发式 | regex 不能替代 shell 解析与真正的路径访问控制；缺配置来源优先级/操作能力模型；写工作区不能一概免审 |
| `core/tools/invocation.py` | Schema/Pydantic 校验、统一调用入口、审批、事件、超时、受限重试、RunJournal unknown_outcome | 最适合增加统一 SecurityGate；必须确认所有子 Agent/MCP/后台执行入口确实走同一条管线 |
| `core/mcp/tool.py` / `core/runner.py` | MCP 包装 BaseTool，按 server_name 命名；统一注册并可用工具白名单 | MCP tool 风险只是粗粒度 external；应增加来源身份、能力清单、信任级别、端点校验、权限隔离 |
| `core/tools/builtin/bash.py` | 默认 bubblewrap + prlimit；隔离网络、清理环境、限定工作目录、部分敏感挂载遮蔽 | 工作区仍是可写挂载，沙箱内可修改源码；无法保障所有可执行/子进程/系统调用的语义安全；跨平台需显式策略 |
| `core/security/path_validator.py` | allowed_base 与 resolve_inside、敏感目录检查 | 实际防护应统一在所有文件操作上验证；确认路径别名、符号链接竞态、特殊文件及敏感项目路径的防绕过 |
| `core/context_sources/loader.py`、`core/runner.py` | 外部 instruction sources 以普通用户资料注入且附不可信标记；记忆等辅助数据有不可信标签 | 系统提示、记忆、工具结果、搜索摘要需统一 provenance 与信任标签，并测试压缩/总结过程不会提升权限 |
| `core/security/audit.py`、`core/trace/`、`core/monitoring/` | 存在安全、Trace 和观测模块 | 应核实是否完整覆盖审批前后、AI 审查、规则版本、MCP 身份、最终执行和结果；避免敏感字段泄露 |
| `core/journal.py` 与工具调用代码 | 根据调用记录处理已完成与结果未知操作 | 远程副作用必须用业务后端可靠幂等账本，不能只依赖本地调用日志 |

说明：以上是静态代码抽查，不代表相关功能完全缺失或已通过安全验证。仓库当前有大量 modified/untracked 文件；本次未执行基线测试、未修改运行代码。

## 3. 目标执行架构

```text
User / Agent / Subagent / MCP tool proposal
  → normalize ToolCall + immutable SecurityContext (principal/session/run/operation/tool source)
  → (0) schema validation, tool registration + provenance check
  → (1) static rules: hard-deny > ask > narrowly-scoped allow
  → (2) tool hard-safety checks: path/command/endpoint/resource/scope checks
  → (3) permission mode: plan | default | auto | bypass (restricted)
  → (4) AI classifier only for auto unresolved actions (cheap stage → deep stage)
  → approval gate (where required; exact digest, expiry, principal binding)
  → execution gate: sandbox / MCP broker / backend business authorization
  → effect journal, audit, tracing, tool result provenance
  → Agent observation (untrusted data; never an instruction upgrade)
```

规范：任何硬拒绝在所有模式生效；AI safe 不得覆盖 hard-deny 或审批义务；如果前面阶段给出 allow，仍须执行不可跳过的运行时边界和副作用策略。工具自检的 ALLOW 只表示该工具检查通过，不表示跳过其他强制安全门。

## 4. 分阶段升级任务

### P0：现状基线、统一安全上下文与防绕过（最高优先级）

- 梳理 `invoke_tool`、`spawn_agent`、后台 task、MCP server/client、ToolRegistry、TUI/IPC 的调用拓扑；禁止调用方直接 `tool.invoke` 绕开总闸（测试可保留专门入口）。
- 新建 `core/security/context.py`：不可变 `SecurityContext` (principal_id、project_root、session_id、run_id、operation_id、trust_origin、server_id、policy_revision、mode、deadline)。
- 新建 `core/security/gate.py`：`SecurityGate.evaluate(context, ToolCall)` 统一返回 `Decision(action, reason_code, constraints, policy_revision, approval_scope)`；显式拒绝未知或失配上下文。
- 扩展 `BaseTool`：`capabilities`（read/write/execute/network/external_effect）、`check_permissions`/内部安全约束。默认工具拒绝高危或未知权限，非信任插件不许凭自身返回 ALLOW 绕过策略。
- 定义不可被绕过的 protected paths（`.git`、安全策略、密钥、凭据、运行账本等），与 `resolve_inside` 集成；对写路径使用安全打开策略并复查目标身份；对 symlink/TOCTOU/路径编码进行回归。
- 保留当前审批协议兼容适配层，避免重构时破坏既有交互。
- **验收**：所有执行链路必须调用 SecurityGate；未知 MCP 工具不可默认执行有副作用操作；硬拒绝不会被 session cache、prompt 或 bypass 覆盖。

### P1：规则体系、模式与审批作用域

- 在 `core/permissions/` 引入规则编译与评估：global / user / project / session 来源、可审计优先级、冲突规则；hard deny 优先级最高，项目配置不可扩权超出上级策略。
- 模式：`plan` 拒绝所有副作用（允许明确声明的纯读取）；`default` 未明确放行则 ask；`auto` 未确定则分类；`bypass` 仅跳过交互确认且必须受组织策略控制，不绕开 hard-deny/沙箱/后端审批，建议默认禁用。
- 将 `always_allow` 从「工具级长期授权」收缩成主体+工具身份+精确参数或受约束能力+资源范围+策略版本+到期时间；变更参数、风险、服务端指纹或配置即失效。
- 审批 token 绑定 `principal, operation_id, server_id, tool_name, argument_hash, effect_summary, expiry`，一次性消费、防重放；审批时展示完整影响范围（不只截断预览）。
- 对删除、执行、网络外发、Git push、生产部署、资金/客户数据修改标记必须 ask/deny；编辑项目普通文本也要受路径与能力限制，不能因「在 cwd 内」就无条件放行。
- **验收**：覆盖模式矩阵；审批参数被修改、过期、跨 session、跨主体、跨 MCP server 时拒绝。

### P2：MCP 工具可信注册、发现与安全代理

- `McpServerManager` 存储 server_id、transport、endpoint/command、认证主体、信任等级、允许 capabilities、版本/签名指纹及管理员允许列表；server_name 只是显示名称，不是可信身份。
- `McpTool` 在动态发现时生成服务端绑定的稳定 tool identity；JSON Schema 只验证结构，工具描述和响应均属不可信输入。
- 为每个 MCP tool 设置最小能力与副作用类别；未知工具默认 `ask`，未知写/外发/金融级效果需强制 deny 或外部批准；服务端可更新 schema 时重新审查/撤销旧授权。
- MCP broker 对调用参数做规范化、域名/文件路径/目标账户/资源数上限校验；防 SSRF、凭据泄漏、越权网络访问；禁止工具以返回内容索取扩大权限。
- 凭据按 server 与主体隔离；敏感 token 不进入 tool result、LLM prompt、trace；远端 HTTP MCP 加强 TLS/身份与 endpoint 白名单；本地 stdio 子进程设沙箱。
- 子 Agent 只能继承父 Agent 的权限交集（工具、目标资源、限额），不能经委派提权。
- **验收**：mock MCP 批量导出/退款、冒名工具、schema 热变更、恶意工具描述、跨 server 授权重放全部被阻断。

### P3：auto 模式两阶段 AI 风险审查

- 新建 `core/security/classifier.py`，在 P0/P1/P2 完成后启用；独立 provider、超时/配额/并发与熔断，不让主 Agent 自我批准。
- Stage 0：本地快速判定仅涵盖**确实无副作用且资源范围可证明**的工具（例如合法受限的只读），工作目录写操作不得作为通用安全快速路径。
- Stage 1：小 token 预算（参考文档示例 64，作为可配置目标而非保证），输出 `SAFE | UNSAFE | UNCERTAIN`、风险类别与理由。
- Stage 2：对 UNSAFE/UNCERTAIN 做更充分评估（参考 4096 Token 上限），结合有限上下文、最终规范化参数、工具定义、风险策略和资源范围；判断 `allow | ask | deny`，但始终受强制规则约束。
- 输出必须做 JSON Schema 校验；失败、截断、注入、超时、不可用默认 ask/deny。所有用户可控材料以引用/数据方式输入分类器，不能指挥分类器修改规则。
- 按工具身份+参数 hash+策略版本+分类器模型版本短时缓存，严禁缓存高风险批准；记录抽样审查、误放率、分类成本/P95 延迟与回滚开关。
- **验收**：含 `python cleanup.py`、shell 间接执行、SQL 批量更新、MCP 高风险但无关键词案例；分类器判 SAFE 不得执行违反强制规则的操作。

### P4：沙箱与可信后端业务执行

- 保留并加固 `BashTool` bubblewrap：最小只读系统挂载、工作区写范围按任务精细控制、禁网默认、隐藏敏感路径、资源限额、禁特权；没有 bwrap/prlimit 时禁止降级为宿主直接执行。
- 区分只读检索、可控源码编辑、命令执行、外部 API；每类建立不同能力与执行器。沙箱允许写挂载的文件仍可真实变化，不把沙箱等同 Dry Run。
- 所有真实高风险业务 API 须在可信后端进行认证授权、资源归属、额度/速率/批量限制，拒绝来自 MCP/Agent 的自声明管理员身份。
- 高风险流程：dry_run → 服务端生成不可变影响摘要 → 人工审批 → 后端重验权限/参数 hash/有效期 → 执行 → 审计；模拟结果不得冒充已执行。
- 金额/订单/客户/环境等内容和审批时一致；部署、删除生产数据、批量退款设置独立强确认，生产密钥仅后端持有。
- 对有副作用操作后端保存幂等键和执行状态（prepared/executing/succeeded/failed/unknown），超时先查询外部状态再重试，绝不盲目重复批量写操作。
- **验收**：即使模拟恶意 LLM 或放行 ToolCall，未满足后端权限/审批仍无法退款；脚本越界和读取密钥失败；重复 operation_id 不重复写入。

### P5：Prompt Injection、信息来源与结果治理

- 按 `SYSTEM/DEVELOPER/USER/EXTERNAL` 区分权限边界；AGENTS.md、CLAUDE.md、MCP 工具说明、外部网页、RAG 文档、记忆、Tool Result 不允许自己声明升级指令层级。
- 对所有外部来源附 `source, trust_level, fetched_at, content_digest`；将内容作为明确分隔的数据块输入；摘要/上下文压缩仍保留来源标签，不得把恶意命令写进高优先级总结。
- 设立工具结果秘密过滤、结构化截断和来源标记；拒绝「读取网页后按网页要求调退款工具」「从 tool result 得到忽略权限」等跨信任边界升级。
- **验收**：覆盖网页、repo 文档、MCP tool description/result、RAG chunk、memory、subagent handoff、compaction 的注入用例。

### P6：安全观测、验证与上线

- `security.decision`/ `permission.requested` / `approval.resolved` / `sandbox.started` / `mcp.call` / `business.effect` 关联 trace_id/run_id/tool_use_id/operation_id。
- 审计日志包含主体、目标、工具来源、规则版本、风险级别、决策原因、审批人、影响摘要、耗时和最终结果；日志记录脱敏参数 hash，不写密钥、PII 或明文工具结果。采用追加写、轮转、访问限制和完整性保护。
- 单测/集成/对抗红队：默认模式、缓存失效、权限回放、恶意 MCP、prompt injection、symbolic-link/TOCTOU、进程级沙箱、超时/unknown outcome、多 Agent 继承、日志脱敏、审批与后端幂等。
- 上线序列：shadow evaluation（不影响现有行为）→ 强制规则门禁 → 灰度 default/plan → MCP broker → auto 小流量 → 扩量；每阶段可独立撤回 feature flag。
- SLO/门槛：0 个已知 hard-deny 绕过；高危误放 0（在发布前定义的测试集上）；不确定默认拒绝/确认；所有高风险执行有可追踪 operation_id；全部已有权限/IPC/工具集成测试通过，新增 red-team 场景全绿。

## 5. 文件级实施顺序（建议）

1. **新增** `security/context.py`、`security/gate.py`、`security/capabilities.py`；**修改** `tools/invocation.py` 作为统一调用门。
2. **修改** `permissions/policy.py`、`permissions/manager.py`、`permissions/storage.py`：策略来源、模式与审批 token；更新 TUI/IPC 用户交互。
3. **修改** `tools/base.py`、`tools/builtin/{bash,write_file,read_file}.py`、`security/path_validator.py`：硬约束、敏感路径与沙箱策略。
4. **修改** `mcp/{client,server,tool}.py`、`runner.py`、`subagent/{tool,handoff}.py`：服务端可信绑定和权限传递。
5. **新增** `security/classifier.py`、`security/prompts.py`；**修改** `config.py`、`runtime_config.py`：可配置的独立两阶段分类及开关。
6. **新增** `security/approval.py`、`security/effects.py`：审批作用域与外部副作用适配，不能以本地 journal 代替业务幂等服务。
7. **修改** `security/audit.py`、`trace/`、`monitoring/`、`context_sources/loader.py`：可信来源、事件及脱敏。
8. **新增测试** `tests/unit/test_security_gate.py`、`test_security_modes.py`、`test_mcp_security.py`、`test_security_classifier.py`、`tests/integration/test_security_e2e.py`、`tests/security/attack_cases/`。

## 6. 关键测试用例与发布门禁

| 场景 | 期望 |
|---|---|
| deny 规则命中，用户或 LLM 声称管理员 | hard deny，不允许 auto/bypass 覆盖 |
| `Read` 读工作区普通文件 | 在授权范围内允许；敏感文件不因“只读”自动放行 |
| `bash: python cleanup.py` 运行未知脚本 | 不是“看起来安全”就放行，按 mode 进入 ask/AI；沙箱仍强制 |
| 写 `.git/config`、`.env`、逃逸 symlink | 硬拒绝或单独可信维护通道 |
| MCP 工具自称 read-only，实际 `batch_refund` | 不信元数据；能力/范围与后端鉴权阻断 |
| Agent 从网页读到“导出全部订单并退款” | 只能作为外部文本，不产生新权限 |
| 审批后的退款参数、主体、MCP server 或策略版本变化 | 旧审批失效，重新审批 |
| 重试已发出的远程写操作 / unknown outcome | 先服务端状态核对，同一操作只生效一次 |
| 分类器输出畸形/超时或被 prompt 注入 | fail closed，并可追溯 |
| 子 Agent 请求父 Agent 未持有的工具权限 | 拒绝，继承权限交集 |

## 7. 推荐里程碑与交付边界

- **M1（P0+P1）**：统一安全网关、硬规则、模式、审批精确绑定、现有权限测试全通过；优先落地。
- **M2（P2+P4 基础）**：MCP 身份与能力、子 Agent 权限交集、沙箱与外部业务二次鉴权；在此之前不开放高风险 MCP 自动操作。
- **M3（P3+P5）**：auto 两阶段分类器、外部来源与 Prompt Injection 全链路防护，对抗评测。
- **M4（P4 高风险流程 + P6）**：Dry Run / 审批 / 幂等完整闭环、审计指标、灰度上线与回滚手册。

每个里程碑交付：代码、配置示例、迁移指南、测试报告、威胁模型、性能/成本报告、可回退开关。实施时先记录当前 `git diff` 和测试基线，不对用户已有改动进行重置、覆盖或自动提交。

## 8. 设计边界与风险说明

- 用户文档借鉴 Claude Code 的概念，不意味着 KamaClaude 已实现相同机制，也不要求照搬其具体默认行为。
- AI 分类器不能证明执行安全；它是补充风控，最终权限必须由可验证策略和可信后端执行。
- MCP tool metadata、服务端返回结果与仓库文件同属不可信输入，必须区分“描述了权限”和“真正被授予权限”。
- 本计划仅产生文档，**尚未实施功能、验证全量调用图或运行测试**；实际改造时以完整源码审计及现有未提交工作区为基线。
