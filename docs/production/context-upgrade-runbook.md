# 上下文升级操作记录与回滚

分支：codex/context-engineering-2026-10-14。
起点 HEAD：7d2fa74d72f2594c95b6fff09df6f1a1962d8675。
在既有大量未提交生产升级修改上叠加；用户已明确要求保留并直接继续。
本次未提交、合并或部署，未调用真实计费模型，未改生产配置或真实会话数据。

## 当前门禁

本批代码尚未完成最终 Linux 回归，不可作为已通过发布验收的版本。
本机 Remote Desktop Commander 执行器中途离线，直接 WSL 启动返回
Wsl/E_ACCESSDENIED。最终 Linux 检查需在执行器恢复后重新运行。
先前中断的验证进程是否退出尚未确认，恢复后应先检查本批验证进程，
不要重复启动多份全量测试。

参见 context-baseline.md 与 context-benchmark.md 区分基线和升级后证据。

## 配置

使用相邻 context-compaction.example.toml 的 [compaction] 段。
默认 shadow 不更改推理投影；逐项开启外置、Microcompact、手动滚动摘要后，
再在隔离会话启用 enabled 与 enforce。未知模型必须显式配置已核实窗口。
实际输出 max_tokens 目前为 Provider 声明的 32768，预算同步读取此值。
不要为绕过门禁而随意填大 model_context_window。

auto_threshold 是兼容旧的响应后触发路径；新链路推荐保持 0，
通过 enabled、high_watermark 和 preflight_mode 管理发送前策略。
不要在未经验证的会话同时启用两套自动触发配置。

## 回归命令

在项目根目录执行（现有虚拟环境已满足依赖时可用 --no-sync）：

    uv run --no-sync ruff check src tests scripts
    uv run --no-sync mypy src
    uv run --no-sync pytest tests/unit -v
    uv run --no-sync pytest tests/integration -v -m 'not integration'
    uv run --no-sync python scripts/gen_protocol_doc.py --check
    uv run --no-sync python scripts/context_benchmark.py

integration marker 在本项目用于真实模型测试；常规集成目录使用 Fake Provider。
未经明确预算安排，不执行真实模型质量或缓存成本测试。

## 备份与迁移

当前只在隔离临时测试目录试验新格式，没有改真实 ~/.claudedock 状态，
因此本次没有伪称已完成生产 SQLite/Session 一致备份。
真实状态迁移前需停止所有写入者，使用已有 scripts/state_backup.py
及项目原有恢复流程建立并校验一致快照；不得直接复制活动 SQLite WAL。

新会话投影按需写入，不覆写 thread.jsonl。旧格式可读。
旧程序的完整回滚需要先从新投影导出兼容推理历史到另一个测试根目录；
不能假设任意旧版本能识别 context_projection.json。
不要将模型摘要投影重新覆盖完整审计日志。

## 软回滚

先关闭 compaction.enabled、microcompact_enabled、tool_result_budget_enabled，
并将 preflight_mode 设为 shadow、auto_threshold 设为 0。
已有投影和引用保留以便恢复；已有外置原文仍应保留到其历史引用不再需要。
关闭外置后模型暂不能通过新读取工具获取旧结果，重新启用读取能力前
仍须遵守工具白名单与权限检查。

出现目标丢失、越权、原始历史损坏或副作用重做时停止灰度。
此时不要用版本回滚假装已经撤销外部副作用，应先查 Journal 的真实状态。

## P1/P2 续作

P1/P2 已按用户要求继续实施，详见 ../architecture/context-engineering-p1-p2.md。
所有新能力保留独立开关且默认关闭；运行时记忆工具的项目范围是可信程序约束。
最终 Linux 测试与 WIRE_PROTOCOL.md 重新生成仍是发布前阻塞门禁。
恢复环境后先执行 uv run --no-sync python scripts/gen_protocol_doc.py，审查生成差异，
再运行 --check、Ruff、Mypy、完整离线 pytest 和两个 context benchmark 脚本。
不要把中间版本或隔离验证的通过记录当成最终发布签字。
