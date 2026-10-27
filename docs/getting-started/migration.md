# ClaudeDock 名称迁移

本次变更将工程标识统一为 ClaudeDock。旧名称只在本迁移说明和安全兼容拒绝规则中保留，历史操作日志、私有备份与 Git 历史不会被重写。

## 名称对应

| 原名称 | 新名称 |
| --- | --- |
| KamaClaude | ClaudeDock |
| Python 包 `kama_claude` | `claudedock` |
| `kama` / `kama-core` / `kama-tui` | `claudedock` / `claudedock-core` / `claudedock-tui` |
| `KAMA_*` 环境变量 | `CLAUDEDOCK_*` |
| `KamaConfig` / `KamaTuiApp` | `ClaudeDockConfig` / `ClaudeDockTuiApp` |
| 项目与全局 `.kama/` 目录 | `.claudedock/` |

旧命令与旧环境变量不再作为公开接口提供。第三方凭证变量，如 `ANTHROPIC_API_KEY`、`ZHIPU_API_KEY` 和 `QDRANT_API_KEY`，沿用各自服务的名称。

## 项目内已完成的迁移

- 迁移 Python 包、导入、CLI/TUI 标识、打包元数据、依赖锁文件、Docker、CI、脚本、测试及文档。
- 将当前项目的私有状态目录整体移动到 `.claudedock/`，不改写其中的历史日志和备份。
- 仅替换当前项目 `.env` 的项目变量前缀和本地默认目录路径，凭证值不变，不写入公开示例。
- 保留 MIT 原始版权声明和 Git 远端信息；本地改名不会自动重命名 GitHub 仓库或更改远端地址。

## 已有安装如何切换

1. 结束或取消旧守护进程中的运行，再停止旧守护进程。
2. 更新源码后执行 `uv sync --frozen`，使用新命令启动。
3. 检查外部启动脚本、终端环境、systemd 单元和 CI Secrets，将项目变量前缀改为 `CLAUDEDOCK_`。
4. 检查已有全局 `~/.kama/`。迁移前自行备份，将其配置、记忆和会话等按需复制到 `~/.claudedock/`，保留私有文件权限；目标目录已存在时先审核后合并，避免覆盖。本次任务不自动改写项目外的用户数据。
5. 同步检查全局配置中的显式路径。临时沿用已有私有配置时，可通过宿主环境中的 `CLAUDEDOCK_CONFIG` 指定原配置文件路径。

新默认路径不会自动发现旧全局状态。Qdrant 派生集合名称也已改名，可根据权威文档或记忆数据重新构建索引；旧集合未被删除。Compose 的状态卷也已改名，已有 Docker 卷未删除；升级部署前应备份并迁移状态卷，或明确配置原卷挂载。工程目录改变可能影响按绝对项目路径隔离的记忆命名空间，迁移时应检查项目归属。

## 操作恢复与安全

包名改变会影响工具实现身份。旧 Journal 的已完成或未决操作不能因此自动获得新身份下的重放资格；请核查资源状态后再发起新的操作。

旧私有目录 `.kama/` 继续保留在敏感路径拒绝规则中，防止升级后工具读取迁移前的配置或凭证。不要为追求文本上零旧名称而删除这项安全兼容保护。

## 清理范围

删除了不符合现有认证和审批 token 协议的 `trace_permission_flow.py`、已被现有架构和运维文档替代的根目录阶段性总结 `IMPROVEMENTS.md`，以及可重建的 Python、测试、类型检查、lint 与覆盖率缓存。

保留测试、评测集、设计与实施记录、依赖锁文件、离线构建缓存、用户配置、记忆、运行记录和私有历史快照。
