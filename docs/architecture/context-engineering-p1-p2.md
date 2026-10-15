# P1/P2 实施说明

状态：首版源代码已接入，默认功能开关关闭；尚未通过最终 Linux 发布门禁。
本文对应 CE-07～CE-13，补充 context-engineering.md 的 P0 数据流。

## CE-07：有来源的指令快照

instruction_sources_enabled 开启后，读取用户全局 ~/.kama/context.md、
项目 CLAUDE.md、AGENTS.md、.kama/context.md、CLAUDE.local.md；
InstructionLoader 也支持显式当前目录沿途的目录规则，不递归扫描无关目录。
导入语法为 @import 相对路径，仅允许 .md/.txt，限制深度、数量、单文件及总字节。
路径先限定根目录，再从根目录逐级以 openat/O_NOFOLLOW 打开。
拒绝越界链接、导入环与被其他用户可写的文件；正文复用既有脱敏规则。

快照记录 source_path/source_hash/scope/precedence/mtime_ns/schema_version，
相同内容去重。诊断通过 context.sources_loaded 事件呈现。
本轮快照进入 checkpoint；后续文件变更只在新 run 重新装配。

新加载的规则放在用户资料消息中，清除旧的全局/项目 system 拼接，
文件名不会提高指令权限。滚动摘要的用户锚点单独从完整 transcript 重建，
不会把外部规则正文错误地当作用户授权或新的工具白名单。

## CE-08：确定性 Session Memory

SessionMemoryService 从真实 run/tool 事件增量维护 session_memory.json，
独立于手写 notes.md，也不自动转存为长期 MemoryStore。
只有成功工具事件进入 completed_verified，助手正文不会生成完成证据。
文件状态仅对已执行文件工具、工作区内可核验的小文件记录摘要；
未知操作与最终结论继续保留核验提示。

每会话复用服务，经过 Supervisor 后台调度，写入使用文件锁与 revision CAS。
旧候选无法覆盖新版本；持久化失败不改变主任务结果。
会话关闭时回收后台任务，恢复时读取最后完整笔记。
这是 v1 确定性服务；可选后台 LLM 整理尚未开启或实现，不产生新增模型费用。

## CE-09：长期记忆来源、时效与项目范围

MemoryEntry 新增 source/source_run_id/verified_at/expires_at/confidence/policy_tags。
SQLite 使用兼容的 metadata JSON 列；旧表升级前用 SQLite backup API 生成
一致快照、校验 integrity_check，再原子提交备份与新增列。
损坏备份会阻止迁移；旧格式没有新字段时使用默认值。
元数据列可被旧版本忽略，但旧版本整体替换行会丢弃这些元数据，需按运行手册回滚。

注入、精确查询、语义召回都重新检查权威 SQLite 记录的状态与 TTL，
过期或无时区/损坏的有效期不会被当作新鲜事实。
源项目参与新记忆 ID，避免同内容跨项目合并；
整合器禁止跨项目合并，并传递来源有效期的最早截止时间。
重新保存过期事实仍是新辅助笔记，不自动获得 verified 状态。

运行时 memory_search/memory_query 始终绑定当前项目及允许的全局记忆；
写入不得由模型切换到另一个项目。程序直接管理 API 仍保留显式跨项目查询能力。
memory_provenance_enabled 增加来源提示与 Token 注入预算，
并为新 fact/knowledge 提供 7/30 天的初始 TTL 上限，显式更短期限优先。
preference/decision 没有统一默认过期日；这些初值仍需真实任务调优。
memory_retire 经正常写操作审批撤销当前范围记忆，不等同于物理擦除。

## CE-11：结构化子任务交接

TaskHandoff 包含目标、预期交付、相关文件、约束、证据引用与程序提供的
工具清单、父运行 ID、预算快照。模型材料不能扩大权限或预算。
子任务仍使用独立上下文与共享的可信 ExecutionBudget。

ResultSummary 限制助手结论长度，区分执行证据、文件摘要和待核验声明。
父侧在前台返回及 agent_result 读取时再次检查当前文件摘要；
文件变化或不可访问会保留 open_issues，不无条件采纳子 Agent 的完成声明。
通过 shell 发生的任意文件变更仍不能仅凭文本穷举，需继续做 diff/测试核验。

## CE-12：稳定缓存分段

PromptText 是兼容 str 的内部载体。稳定策略/项目前缀与目标、笔记等动态后缀分开，
缓存标记仅落在稳定边界。TracingProvider/GovernedProvider 保留旧接口。
代理明确拒绝缓存语法时，有限移除标记重试；认证和普通参数错误不触发此回退。
context.cache_usage 记录稳定前缀 hash、真实 cache read/create 数量。
尚无真实 API 的缓存命中率、成本净改善或统计质量结论。

## CE-10：按需工具发现

tool_schema_deferred 开启后，在每 run 已经完成白名单过滤的 Registry 中注册 tool_search。
基础高频工具始终加载，其余只返回名称、短说明、风险级别和是否加载。
resolve 只接受已有授权名称，逐步解锁本轮 Schema，不能注册新工具或扩大权限。
冻结快照、稳定排序与 schema_hash 支持缓存；运行中注册/替换 Schema 被拒绝。
父子 Agent 都应用此策略，角色白名单缺少 tool_search 时保持原有允许工具集合。

scripts/context_tool_benchmark.py 提供 10/50/200 合成工具分档测量。
只有真实模型选择准确率与总费用评估通过后，才能考虑默认启用。
SkillLoader 与 use_skill/read_skill_resource 保持既有渐进加载流程，没有建立第二套 Skill 系统。

## CE-13：TUI 与观测

新增 /context、/notes，以及 /compact focus。
认证 IPC session.get_context 只返回脱敏笔记、来源和投影元数据。
预算、裁剪、压缩开始/完成/失败、记忆更新与缓存统计均有判别联合事件。
完成事件区分摘要体积与完整投影体积，保留近期消息数和修订号；
成本节省不能错误地用“原上下文减摘要正文”计算。
统计标签不携带正文、session/run ID、文件路径或前缀 hash。

协议生成器同时枚举此前未列出的所有 typed Command/Result/Event，
需在 Linux 环境恢复后重新生成并 --check WIRE_PROTOCOL.md。

启用对应 P1 开关后，长期记忆、自动会话笔记以及文件派生的检索/技能清单进入
低信任用户资料消息，保留其数据身份，不再混入 system 策略。
原始用户请求从独立审计轨道提取并作为压缩锚点，辅助资料不会伪装成用户授权。
