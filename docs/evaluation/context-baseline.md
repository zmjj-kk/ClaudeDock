# 上下文工程基线

日期：2026-10-14 开始，2026-10-15 继续。
HEAD：7d2fa74d72f2594c95b6fff09df6f1a1962d8675；
原分支 codex/production-upgrade-2026-10-14，工作树已有大量未提交修改。
实施分支 codex/context-engineering-2026-10-14，保留这些修改。
不记录 .env、密钥或真实会话正文。

## 环境与证据

WSL Ubuntu-22.04；系统 Python 3.10.12；
项目虚拟环境 Python 3.12.13；uv 0.11.18。
现有环境可直接 --no-sync 运行。初始 --offline 方式触发本地包重新构建，
因 hatchling==1.29.0 不在缓存而失败；没有因此更改依赖锁文件。

升级接入前已有测试：526 passed, 1 deselected，17.84 秒。
真实模型 integration marker 被排除，无真实计费模型调用。
初始 Ruff 全量检查通过；Mypy 对 132 个源文件通过。
原始协议 --check 通过。

较晚的基线自动记录已包含刚创建但未接入的模块：
results.json 的 ruff=1 对应这些新模块的排版错误，
不能冒充原工作树已有缺陷。该轮 mypy=0、pytest=0、protocol=0。
后续这些排版错误已经修复。

原始日志位于 /tmp/kamaclaude-context-baseline/：
preexisting.patch、ruff.txt、mypy.txt、pytest.txt、protocol.txt、results.json。
临时目录不是长期备份，且 patch 不是完整未跟踪文件快照。
没有宣称完成真实生产状态备份。

## 固定任务集

tests/fixtures/context_eval/tasks.json 包含 20 条无个人信息场景：
长期编辑、约束变更、工具噪声、中断恢复、指令注入各 4 条。
每条有原始目标、硬约束、未完成事项与已验证动作真值字段。
这些是固定场景与确定性回放输入，尚未完成真实模型 A/B 质量评审。
