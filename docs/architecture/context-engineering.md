# 上下文工程：首批核心实现

状态：开发中，尚未发布。对应 2026-10-14 方案的 PR-01～PR-06 基础链路。
自动摘要、工具结果外置和 Microcompact 默认关闭；预检默认 shadow。

## 数据流

ExecutionContext → 请求级 ContextBudget → 旧引用结果裁剪 → 滚动摘要候选 →
完整历史归档 → 投影版本提交 → Provider → 工具调用 → 有限预览/私有原文引用。

ExecutionBudget 仍负责整个 run 的累计模型调用、费用、时长、工具次数。
ContextBudget 仅计算一次请求：系统文本、完整工具 Schema、完整消息、
实际输出预留和安全边际。未知窗口在 enforce 模式下安全拒绝。
当前回退计数是 UTF-8 字节上界，报告 estimate_method=utf8_upper_bound；
它偏保守，不能拿其数值声称真实模型 Token 或费用节省。供应商 tokenizer、
实时计数与 usage 校准尚未实现。

## 历史与投影

thread.jsonl 保留完整可见历史以及工具结果引用。历史查看使用 read_history；
推理恢复使用 read_messages。首次无投影的旧会话沿用兼容读取。

compact/projection_N.json 保存不可变候选；context_projection.json 是原子提交指针。
候选包含 schema_version、revision、archive_count、messages、digest。
写入在投影锁中进行，支持 expected_revision CAS。
摘要内容与工具配对先验证；原子切换指针后才更新内存上下文。
最新投影损坏时尝试指针所记载的上一完整版本；两者均损坏则明确失败。
不得将损坏投影无声替换成任意摘要。

run checkpoint 同时携带 audit_messages、audit_start。
归档消息使用 run_id 加消息序号作为幂等标识。压缩前先归档；
中断恢复可追加缺失审计记录，重复恢复不应重复追加。
旧 checkpoint 没有审计副本时不宣称能够重建历史上从未保存的原文。

## 工具结果

外置在 invoke_tool 的成功返回边界完成，早于成功事件与 Journal 记录；
因此巨量正文不会在这些层无意重复保存。私有原文采用原子写与摘要校验，
引用绑定实例所属的会话或子 run。读取工具通过原有权限管理器检查，
不接受模型指定任意文件路径。

模型获得有限预览、结果 ID、字节数与 digest；原文按字节分页。
原文文件为 0600，目录新建为 0700。读操作检查 ID、软链接、TTL 与 SHA-256。
存储失败返回“工具已执行、输出保存失败”，禁止据此自动重做副作用。
子 Agent 使用独立结果目录及相同配置，工具白名单仍限制读取工具注册。

限制：当前 ToolResult 协议仍以字符串为主；配额是本地存储上限，
暂不自动回收会话正在引用的文件。整轮预算限制可见正文，引用的固定协议开销
还会由请求级 preflight 计入。非文本媒体、并发磁盘配额原子预留、
按会话读取次数上限仍需后续验收。

## 可信边界

用户目标保留在可信运行字段中。滚动摘要保留用户原始请求与近期完整轮次，
LLM 派生摘要显式标记为待核验信息。严格路径要求完成动作和文件状态声明
引用提供的成功工具结果；不存在证据的“测试已通过”不能只凭六个标题提交。
该校验不能证明任意自然语言结论都真实，仍需固定任务集和质量评估。

存在 prepared、executing 或 unknown 操作时拒绝摘要替换。
thinking/signature 按完整工具组保留，不伪造签名。
磁盘提交失败保留原内存投影，候选文件不代表已提交。

## P1/P2 续作

P1/P2 首版已接入，详见 context-engineering-p1-p2.md。
当前仍在开发分支，所有最终 Linux 门禁完成前不发布。
