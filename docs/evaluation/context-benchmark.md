# 上下文工程验证记录

状态：P0 核心及 P1/P2 首版代码已接入，发布验收未完成。
不能把“源码已实现”或“隔离算法通过”写成全量运行时验收通过。

## 已完成的证据

- 升级接入前基线：526 passed、1 deselected，17.84 秒。
- 中间 P0 版本：Ruff 通过；Mypy 对 137 个源文件通过。
  这些结果早于后续 P1/P2 和最后的 P0 修订，不代表最终代码通过。
- 新代码的 Python 3.12 语法解析与新增函数中文注释检查通过。
- 无第三方运行时的实际算法模块：系统/Schema/历史预算、中文保守估算、
  未知容量、Mock Provider 探测、容量错误分类、10/30/100 轮裁剪与签名保护通过。
- Windows 隔离加载真实 MemoryEntry、MemorySqliteStore、MemoryStore 实现：
  实际 SQLite 旧表迁移、一致备份、连接关闭、TTL、项目身份与注入预算通过。
  未初始化网络、模型或真实用户数据库。
- 同一隔离环境下，真实 PromptText/ExecutionContext 的缓存分段、
  TaskHandoff 权限字段与父侧过期文件摘要检查通过。
- 真实 Registry 逻辑在 10/50/200 工具规模下的精确名称发现、解锁、
  稳定 hash 和未授权名称拒绝通过。此隔离试验替换了 JSON Schema 验证器，
  因而不属于完整注册/协议验证。明细见 context-tool-estimates.json。
- context_benchmark.py 的 20 条固定回放产生 context-offline-results.json。

## 尚未完成的门禁

Linux 执行器中途离线，直接 WSL 启动返回 Wsl/E_ACCESSDENIED。
Docker 已尝试启动，但 dockerDesktopLinuxEngine 未出现，无法接替运行测试。
Windows 本机缺少匹配的 Linux 二进制依赖；
尝试直接使用纯 Python Mypy 时缺少 librt.internal，未得出最终类型检查结论。

最终 Ruff、Mypy、完整 pytest、Textual Pilot、新版 JSON-RPC、Linux openat/权限、
fcntl 并发锁、断电故障注入、覆盖率门禁均需 Linux 环境恢复后完成。
新增 pytest 覆盖指令环/越界/密钥、Session Memory CAS、记忆隔离/TTL/撤销、
SQLite 迁移备份、缓存回退、子任务状态、工具冻结与 TUI 视图；不声称已全部执行。

WIRE_PROTOCOL.md 的最终生成与 --check 尚待执行。
生成器已修复为包含全部 typed models，旧生成文件因此需要更新，
不能使用早期的协议检查通过记录替代最终检查。

中断测试进程的 Ctrl+C 被自动审批拒绝（该类沙箱审批被禁用），
阻塞进程是否退出未得到确认。恢复执行器后先检查本批验证进程，
再顺序重跑，避免重复创建全量测试进程。

## 不作出的结论

没有真实 tokenizer 误差、真实计费、缓存净收益、模型任务成功率或质量非劣数据。
没有完成 90% 分支覆盖率、kill -9 全路径或多 Agent 长时故障演练。
合成精确名称命中率不能代表自然语言工具选择质量；
utf8_upper_bound 数字不能等同于真实模型 Token。

## 待完成的后续范围

P0：供应商实时计数与 usage 校准、完整结构化 Summary/TaskAnchor，
跨 run 会话熔断、分段摘要、并发磁盘配额预留与完整故障注入。
P1：可选后台 LLM 笔记整理、进一步记忆检索质量与缓存成本实测。
P2：真实模型 10/50/200 工具任务集 A/B 与实际总成本评估。
当前独立功能开关均默认关闭，preflight 默认 shadow，不默认启用自动摘要。
