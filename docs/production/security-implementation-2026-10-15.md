# 安全升级实施与验收报告

依据：[安全升级计划](security-upgrade-plan-2026-10-15.md)。日期：2026-10-15。

本次交付是可运行的本地安全控制与受信业务后端接入边界，不代表真实业务系统、生产灰度或实机沙箱已经验收。没有调用外部 LLM、MCP 业务工具、退款或部署 API；没有发布、推送或改写生产配置。

## 基线与提交范围

- 工作前有大量 modified/untracked 文件。未重置、清理或覆盖用户成果。
- 私有基线位于 WSL 的 `/tmp/claudedock-security-baseline-20261015/`：原始 tracked.patch、status.txt、workspace.tar.gz 和基线测试日志，目录与文件禁止其他用户访问；该快照含本地配置，不能上传。
- 原始测试因 tools 包 eager import 与 tool_result_store 循环依赖而收集失败。修复后基线为 568 passed / 2 failed；两项是 LLM 事件顺序和已迁移投影存储的旧故障注入断言。
- 安全实现建立在已有上下文、生产恢复、监控、权限与存储修改之上，提交包含这些已经通过回归的依赖。密钥、运行数据、私有快照、wheel 下载与缓存均不提交。

## 计划逐项落实与边界

| 阶段 | 本次实现 | 尚需环境或业务支持 |
|---|---|---|
| P0 | 冻结 SecurityContext、统一 SecurityGate、Decision、能力、自检；普通注册表也冻结 schema/工具身份；调用图 AST 回归；路径/软链/硬链接/描述符安全打开 | 同一操作系统用户的恶意进程与任意 Python 插件不在进程内安全边界内 |
| P1 | plan/default/auto/restricted bypass；分层 deny > ask > 受信精确 allow；项目不能扩权；缓存绑定主体/参数/会话/工具身份/策略；一次性审批 nonce、连接归属、期限与审批后重验；TUI 完整脱敏参数 | 旧客户端需与 daemon 一起升级以回传 approval_token；资金等审批交互不会复用泛化本地确认 |
| P2 | MCP 配置与连接指纹绑定身份；显式能力授予；未授信工具默认 deny；每次执行前重新发现 schema；URL/身份覆盖/越界资源/批量上限独立检查；子 Agent 权限交集 | HTTP/TLS 远程代理、可验证签名与真实资源归属代理未接入；普通 TCP 只允许字面 loopback；不支持 URL 的一般网络白名单 |
| P3 | 独立 API key 与客户端，无工具；两阶段严格 JSON 审查；超时、配额、并发、熔断；低风险短时缓存；遥测记录阶段/请求数/延迟 | 默认关闭，未验证在线模型误放率、供应商成本或在线 P95；不能据离线用例宣称生产 AI 风控达标 |
| P4 | Shell 默认只读工作区、明确子目录可写能力、递归敏感路径遮蔽、禁网、资源限制、drop capabilities；无 bwrap/prlimit 或显式 sandbox=False 经总闸拒绝；PreparedEffect/BusinessBackend/EffectExecutor 合约，unknown outcome 查询后端而不盲重试 | 当前 WSL 缺 bubblewrap，只有失败关闭和挂载参数验证；真实进程隔离尚未验收。未提供任何真实退款/部署后端；external_effect 一律拒绝。业务幂等与审批必须由后端原子持久化 |
| P5 | 仓库/全局资料、记忆、RAG、技能、笔记移出 system，作为用户数据附 source/trust/digest/fetched_at；模型发送副本中的工具结果脱敏并标来源；压缩摘要保持外部衍生数据身份 | 这是权限边界加固，不是对 LLM 不受任何 prompt injection 影响的证明；主目标与实际权限仍由宿主工具门强制 |
| P6 | security.decision 与执行结果关联 operation/run/tool/principal；专用摘要审计、0600、锁、fsync、哈希链与轮转；48 个新增安全场景；迁移、威胁模型与离线基准 | 未执行真实业务灰度、容器构建、远程依赖漏洞库审计、在线成本测试或集中审计归档；生产放量须独立验收 |

## 验证结果

- 全量 `pytest tests -q`：**617 passed**。
- 单测覆盖率门禁：**67.16%**，高于现有 65% 门槛；596 个单测通过。
- `ruff check src tests scripts`：通过。
- `mypy src`：156 个源码文件通过。
- `scripts/gen_protocol_doc.py --check`：通过，WIRE_PROTOCOL.md 已重新生成。
- `bandit -r src -ll`：没有中/高严重性问题；低严重性提示不属于此门禁的通过声明。
- 新增对抗回归：四模式 hard deny、敏感路径/软链/硬链接、上下文失配、漏传 manager、强制 ask 与 SAFE 冲突、跨主体/参数/服务端 token 重放、过期审批、恶意 MCP 描述、schema 热变更、SSRF 目标、批量操作、分类畸形/超时/高风险误放、工具调用图、真实文件编辑、审计链和外部结果未知不重试。
- 现有权限、IPC、TUI、恢复、压缩、MCP 与工具重试测试均保留；执行器单测显式模拟人工审批，不再依赖省略权限管理器的旧行为。

## 性能与成本

详见 [离线基准](security-offline-results.json)，重跑：`uv run python scripts/security_benchmark.py`。

本次 1000 次纯读取网关评估中位数 0.2174 ms，P95 0.3137 ms。仅测静态门；不包含文件读取、fsync 审计、MCP、沙箱或 AI。外部模型请求 0、真实业务副作用 0；因此没有在线 AI 费用或在线延迟结论。

## 威胁模型

信任：宿主 Python 代码、受信全局配置、操作系统账户与独立业务后端。当前本地 principal 使用 OS 用户；IPC 认证与审批连接归属是额外约束，不宣称支持多租户身份系统。

不信任：工具参数、仓库与网页内容、MCP schema/描述/返回、RAG、记忆、技能、派生摘要与 Agent 的管理员自述。Python 插件在宿主进程中执行任意代码不能被 BaseTool 类型系统隔离，应另进程运行。

强制不变式：hard deny 先于模式/缓存/AI；外部资料不产生权限；未知能力不能自动放行；高风险执行/网络/MCP 要求逐次批准；敏感输入不外发分类器；真实高风险业务在没有可信后端时禁止调用。

本地哈希链用于发现记录修改与关联轮转，不能抵抗掌握相同 OS 账户或 root 权限的攻击者重新生成整个链；生产应外送至独立不可变审计存储。

## 迁移与运行

1. 备份当前工作区与全局配置。部署时 daemon 与 TUI/CLI 必须使用同一版本。
2. 将 permission/MCP 授权从项目 `.claudedock/config.toml` 移到 `~/.claudedock/config.toml`，参考 [配置样例](security-config.example.toml)。全局配置与策略文件禁止软链接及组/其他用户写入。
3. `.env` 仍可配置普通运行参数，但不能设置 CLAUDEDOCK_CONFIG、CLAUDEDOCK_PERMISSION_MODE 或独立分类凭据；这些值必须来自宿主启动环境。
4. 新的显式规则使用 policy.toml 的 `[[rules]]`，字段为 source 隐含于文件来源、tool、action 和可选 argument_hash。项目/session allow 不授予新权限；全局/user allow 必须绑定精确工具名与参数 hash。
5. 初始使用 default；只读分析可用 plan。安装并实机验收 bubblewrap/util-linux 后再启用命令执行。默认 Shell 只读；文本修改通过受控 write_file 进行。
6. MCP 不再以发现即授信。只在全局配置中显式授权具名工具能力；升级后旧授权不跨服务端连接、schema 或策略变化复用。
7. 在离线对抗集通过后，小流量设置 auto 与 classifier_enabled、独立模型和宿主环境密钥。不确定/失败仍需确认；高风险操作不会进入 AI 自我批准路径。

规则示例：

```toml
[[rules]]
tool = "write_file"
action = "ask"

[[rules]]
tool = "*__batch_refund"
action = "deny"
```

## 回退与发布门槛

可独立关闭 classifier_enabled、切回 default/plan、撤销 MCP tool_capabilities 或恢复已备份的配置。硬路径、硬拒绝、缺失沙箱拒绝执行等不提供绕过开关。回退配置后策略摘要变化使旧缓存失效。

当前发布条件是本地代码回归通过；尚不满足“生产沙箱、在线模型、真实业务后端与灰度放量全部验收”。在缺失 bubblewrap 的当前环境，命令和 stdio MCP 会安全失败，不会降级宿主执行。正式发布前必须补充真实沙箱逃逸测试、可信业务后端鉴权/审批/幂等验证、外送审计与在线风险评估。
