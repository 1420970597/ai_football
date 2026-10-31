# 恢复顺序、端口及容器归属核验

更新时间：2026-10-27 18:34:44 UTC。范围：仅本项目代码及本机 Docker 只读元数据；没有执行旧服务回退、生产恢复、远端目录访问或清理。远端全部服务的归属尚未取得独立核验，不能把本机容器标签当成远端所有权证据。

## 归属与状态

下表来自本次 `docker ps -a` 及限定名称的 `docker inspect`。只读取名称、状态、Compose 项目/服务标签及端口绑定，不读取环境变量、凭据或业务内容。两条检查命令均退出 0。

| 对象 | 可核验归属 | 观察状态 | 处理规则 |
| --- | --- | --- | --- |
| 当前网页、API、MCP、Hermes、审核工作器、采集器、执行器及存储隧道 | Compose 项目 `ai-football-v2`，各自服务标签一致 | 运行 | 保留；发布只操作显式服务，不能按名称模糊匹配停止 |
| 当前迁移服务 | `ai-football-v2` / `migrate` | 已退出 | 预期的一次性任务；退出码应由本次迁移证据另行核验 |
| 旧网页容器、旧推理容器、历史接口修复容器 | `ai_football` / `web-console`、`inference`、`analytics-api` | 已退出 | 保留作历史依据；旧推理服务不属于新业务恢复范围，不重新启动 |
| 旧审计网页、接口及 Redis | `ai_football_audit` | 已退出 | 另一个审计项目；不因旧项目回退停止或删除 |
| 独立验证 PostgreSQL `ai-football-v2-pgverify` | 无 Compose 项目/服务标签 | 运行 | 归属不可仅靠名称推断；保留，任务测试另建临时 PostgreSQL |
| 历史 Android 构建及验证容器 | 多个无 Compose 标签的一次性名称 | 已退出 | 未完成单项归属/保留审核，不能批量清理 |
| 本次故障验证临时 PostgreSQL 与验证器 | 本次运行脚本中的唯一进程编号名称、临时内部网络 | 仅测试期间运行 | 只由该脚本 trap 按完整名称清理本次资源，不处理其他测试任务 |
| 远端数据库与对象存储服务 | 已有连接/数据证据不等于容器或进程归属 | 本次未重新盘点 | 保留；完整 P01-05 仍缺远端服务归属与留存状态的只读核验 |

## 端口冲突清单

| 服务 | 已配置宿主绑定 | 当前判断 |
| --- | --- | --- |
| 当前网页 | `0.0.0.0:3003 → 80/tcp` | 正在提供业务入口；旧服务不得占用或覆盖 |
| 当前 API、MCP、Hermes、采集器和存储隧道 | 无宿主发布绑定 | 内部容器端口不是可占用的宿主入口；不得为了回退临时暴露 |
| 旧网页/推理/接口修复 | 空绑定对象 | 已退出且没有可恢复入口证据，不推测其旧端口 |
| 旧审计网页 | `127.0.0.1:13005 → 80/tcp` | 容器已退出；归属审计项目，恢复时仍视为保留配置，不抢占 |
| 旧审计接口 | `127.0.0.1:18005 → 8000/tcp` | 同上 |
| 独立验证 PostgreSQL | 空绑定对象 | 只在其容器网络可见；不映射生产或宿主默认数据库端口 |

实际切换前必须重新读取绑定。相同协议/端口即使绑定地址不同也按潜在冲突处理；不通过杀死未知占用者来消除冲突。旧只读网页如需要启动，先明确一个无冲突入口及用户切换范围，不能修改当前 3003 绑定代替核验。

## 可审查恢复顺序

既有 `infra/recovery/operations.py` 生成依赖顺序及门禁，本说明补足人工执行语义。每一步依赖上一项的明确证据，失败即保留现场，禁止进入后续写入服务。

1. 核对完整备份的可读性、摘要、数据库结构、媒体清单及独立密钥引用；只使用已验证恢复的快照。
2. 确认目标身份与本项目归属、当前入口状态；计划切换时保留只读展示并停止本项目写入入口，不处理无关项目。实盘执行始终默认关闭。
3. 重新取得容器/进程归属和端口占用清单，核对存储隧道目的身份。缺少标签、归属冲突或端口冲突属于阻断。
4. 在空的隔离数据库及独立媒体前缀恢复。既有业务数据目标一律拒绝；仅允许已明确初始化的迁移种子行，不允许覆盖使用过的调度预算。
5. 比较恢复前后用户/权限、原文/媒体哈希、不可变报价、结算台账、订单和预留本金；重建原始台账后收益差异必须为零。
6. 新平台按 API → MCP → Hermes → 审核工作器顺序启动，分别验证就绪与依赖可用。旧项目兼容回退仅验证旧存储、接口和授权，不启动旧算法、训练或推理模型。
7. 确认检查点与会话唯一归属后只启动一个采集所有者，核验新消息持久化及断点推进，不重新从历史起点覆盖当前投影。
8. 验证只读比分、专家原文、盘口、推荐与统计，确认入口无冲突后才进行明确范围的展示切换。失败保持原入口或撤回本次入口，不能向已有业务库回灌旧数据。
9. 查明所有未知订单及未释放敞口，保持单次提交与账户隔离；没有针对实盘的明确授权，不开启执行器真实交易。

## 验证与节点状态

- `tests/test_recovery_gates.py::RecoveryOperationsTests.test_recovery_plan_preserves_other_projects_and_orders_dependencies` 验证阶段依赖、其他项目保留、端口冲突、非空目标与旧模型禁止重启。已有冻结恢复门禁记录退出 0；本次没有修改该逻辑。
- `.runtime/recovery-seed-20261027/frozen-suite.log`：14 项 / 22.772 秒 / 退出 0；45 张表与 1 个合成媒体，恢复前后财务差异为空；调度预算非默认值准确恢复，使用过/重置后的目标拒绝。
- `.runtime/hermes-telegram-20261027/full-freshness-final-3.log`：693 项 / 257.085 秒 / `OK`；主线确认进程退出 0。仅是冻结隔离样本，不是生产灾备演练。
- P01-04 的“建立顺序与端口冲突清单”已有本说明及门禁证据，可将此设计/隔离验收范围与真正生产回退验收分开标记；不声称执行过回退。
- P01-05 的本机容器部分已核验，完整节点仍缺远端服务归属清单与未知验证资源留存归属确认，应保持未完成。

观察命令：

```sh
docker ps -a --format '{{.Names}}\t{{.State}}\t{{.Ports}}\t{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.service"}}' --filter name=ai-football
docker ps -a --format '{{.Names}}\t{{.State}}\t{{.Ports}}\t{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.service"}}' --filter name=ai_football
docker inspect --format '{{.Name}}|{{.State.Status}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}|{{json .HostConfig.PortBindings}}' ai-football-v2-web-1 ai_football_web_console ai_football_inference ai-football-task42-repair ai_football_audit-web-console-1 ai_football_audit-analytics-api-1 ai_football_audit-redis-1 ai-football-v2-pgverify
```
