# 本机发布：识别修复、迁移 035、镜像统一与静态压缩（2026-11-11）

编号：TASK-78-LOCAL-RELEASE-20261111。授权：用户 2026-11-11 “批准自动部署到生产环境与自动提交、合并远程仓库”。冻结源码 `f82a609a`（集成分支 `feat/TASK-78-ux-backend-expert-hardening` 的祖先；其后的合并只涉及测试、文档与采集器，不改变本次两个镜像的输入）。

状态口径：已发布并做了只读后验。没有做真实模型回归，也没有为验证发起任何交易。

## 1. 发布内容

| 服务 | 发布前 | 发布后 | 带来的变化 |
| --- | --- | --- | --- |
| api | `sha256:d0f13e9dd4a0…`（`e6a4db5d` 加两个补丁文件） | `sha256:4964521338d8…` | 识别修复 C1/C2/C3；`/public/signals` 会话只解析一次；迁移 035 |
| review-worker | `…history-localdb-schema-2d2a88e5` | 同上候选 | 同上；并补齐它此前缺的 034 及 11-11 的各项修复 |
| local-mcp-api | `…history-localdb-tools-2d2a88e5` | 同上候选 | 同上；并补齐此前停在 11-10 中午的审核工具代码（`fa8f97c0`、`36ab2f5c`、`40477ed7` 等） |
| web | `sha256:1305e1ca5efc…` | `sha256:ab30da9056d5…` | 仅 `nginx.conf`：静态资源与公开 JSON gzip |

未发布：网页首屏去重与按需加载（R2a，尚未合并）；采集器的纯搬移（R4）。

### 发布前发现并一并消除的隐患

只读探针（各容器内用自己镜像的 `check_schema` 对生产库比对）：review-worker 与 local-mcp-api 的镜像只有 32 个迁移，库里已有 33 个（多 `034_manual_betting.sql`），`schema_matches_image=false`。两者重启策略为 `unless-stopped`，一旦重启会因结构校验失败而无法启动，专家审核会停。统一镜像后三者均为 34 == 34。

## 2. 镜像与校验

- 构建方式沿用既有做法：以线上 API 镜像为基础，只覆盖有差异的文件，避免未锁定的传递依赖漂移。`Api.Dockerfile` 覆盖 8 个模块并新增迁移 035；`Web.Dockerfile` 只覆盖 `nginx.conf`。构建 `--network none --pull=false`，退出 0。
- 保留的失败：首次 API 构建以 `FROM sha256:…` 书写，BuildKit 把它当作远程仓库解析，退出 1（`build-api.attempt1-failed.log`）；改用指向同一镜像 ID 的本地标签后通过。
- 离线校验（`--network none`，CPU/内存受限）：API 镜像内 130 个源码与迁移文件、web 镜像内 12 个文件与冻结源码逐一相同；父镜像层为候选层的前缀；镜像配置仅 Labels 不同；`pip freeze` 摘要与线上 API 相同；运行用户 10001；`nginx -t` 通过。
- 预检：候选镜像在 api、review-worker、local-mcp-api 各自真实配置下导入与配置加载通过（一次性容器，api 一项无网络）。
- Compose：以 web 容器的配置链为基础，叠加只含 `image` 的覆盖文件；渲染结果相对当前仅 5 个 `image` 字段不同，api、web、review-worker、local-mcp-api 的 config-hash 与运行中一致。

## 3. 测试依据

- 完整冻结套件：2342 项，495.4 秒，退出 0（树 `890e8da4`，服务代码与 `f82a609a` 相同）。
- 合并结果上的分包回归：`feat-r5-merge` 11 个模块 140 项退出 0；审核链路 476 项退出 0（见 `EXPERT_RECOGNITION_FIX_20261111.md`）。
- 压缩边界：`tests/web/compression-check.sh` 退出 0；发布脚本的同一段检查对旧 web 在压缩项失败、对候选 web 镜像（私有子网与替身上游）四项通过。

## 4. 发布脚本与审查

`rollout.sh`：守卫（镜像存在、api 仍为预期镜像、配置链未变）→ 并发删除冗余索引 → 审核空闲闸与账户空闲闸 → 登记迁移 035 → 重建 api 与 local-mcp-api → 重建 review-worker → 轮询验证 → 放闸。失败或被中断时撤销 035 登记并把三个服务恢复到发布前的 API 镜像；撤销失败则保留候选。

独立审查共派出四次，其中两次受上游错误影响：

| 审查 | 结论 | 处理 |
| --- | --- | --- |
| 首轮（全量） | 524 超时，无产出 | 拆成两个窄范围审查 |
| 闸脚本 | 修完再用：缺 `idle_in_transaction_session_timeout`（两个参考实现都有） | 已补；清理顺序、关闭兜底、重试条件一并改 |
| 发布脚本（旧版） | 三条必须修：撤销登记未检查退出码；固定等待 8 秒后单次判定，且管道在 pipefail 下可能误判；无信号处理。随后两次因上游错误中断，未给完整结论 | 重写；其余问题由集成者实测 |
| 发布脚本（新版） | 修完再执行：登记过程中被中断时标志未置位；回滚中再被中断会留下混合态 | 已改；闸保持上限 600 → 900 秒等加固一并改 |

替身演练：用替身 docker/compose 在无网络容器内执行真实脚本的 12 条路径（成功、验证失败、启动失败、撤销失败、迁移失败、闸未取得、守卫拦截、切换中终止、持闸后挂断、登记中终止、回滚中信号、迁移与撤销同时失败），退出码、最终镜像、迁移登记与闸残留均符合预期。演练只覆盖脚本控制流。

## 5. 执行记录（UTC）

| 时间 | 事件 |
| --- | --- |
| 12:40:39 | 守卫通过 |
| 12:40:47 | 并发删除 `v2_current_quotes_active_idx`：发布前存在，删除后不存在 |
| 12:42:37 | 双闸取得（等待 1 个运行中的审核任务结束，约 110 秒） |
| 12:42:45 | 迁移登记，`applied_count=1` |
| 12:43:06 | api、local-mcp-api 就绪 |
| 12:43:21 | review-worker 启动 |
| 12:43:30 | 三个服务验证通过 |
| 12:43:33 | 放闸；`rollout result=0`，未触发回滚 |
| 12:46:48 | web 新容器启动；12:47:05 `web switch result=0` |

API 重建发生在 12:42:45 至 12:43:06 之间，不可用窗口不超过约 20 秒；这是按日志时间推断的上限，没有直接测量。

## 6. 发布后证据（只读）

- 容器：API 发布只改变 api、local-mcp-api、review-worker 三个容器 ID，web 切换只改变 web；其余容器未变。四者镜像为候选、重启 0、config-hash 与计划一致；闸容器无残留。
- 结构：三个容器内 `check_schema` 均通过，34 == 34；冗余索引已不存在。
- 日志：api、local-mcp-api、review-worker、hermes-mcp 发布后无 Traceback/ERROR；nginx 无 error。
- 接口：经 web 访问 `/healthz`、`/public/experts`、`/public/signals`、`/public/matches` 均 200；未重启的 hermes-mcp 访问新 local-mcp-api `/readyz` 为 200。
- 账户：切换后刷新任务 8 次全部成功，校验年龄 3 秒，`healthy/ready`；在途订单 0、未知 0。
- 压缩（宿主端口实测）：`app.js` 103900 → 28738 字节；首页 46568 → 10300；`/public/matches` 462577 → 47603；`/public/experts` 11235 → 3108。`/api/v1/auth/me`（401）与 `/healthz` 不压缩，符合设计边界。

### 证据的限度

- 切换后最初 3 分钟完成的 7 条审核均为闲聊、推广或感叹（逐条核对了模型说明），工具调用为 0；此前 3 小时零工具审核占 74.9%，不异常。
- 12:49 UTC 复查（切换后约 6 分钟）：新的审核工具 API 已被真实审核使用，`mcp_search_matches` 12 次、`mcp_list_match_markets` 9 次、`mcp_resolve_match` 2 次、`mcp_record_expert_signals` 4 次，全部 `succeeded`；新录入推荐 4 条；三个新容器日志无 Traceback/ERROR。
- C1 在生产上的首批实例：任务 `0dfbe4b1…` 读盘遇 `upstream_cooldown`（采集器提示 57 秒），按新逻辑排期 65 秒后重审，第二次审核成功并录入 1 条推荐；任务 `4be73dfe…` 遇 `refresh_throttled`，20 秒后重审成功并录入 2 条；任务 `6a26c081…` 遇 `upstream_cooldown`（提示 34 秒，排期 65 秒），复查时第二次审核仍在进行，结果未知。发布前这类冷却回执会让任务直接以 unresolved 结束。样本只有 3 个任务，不足以评估恢复率。
- 发布开始到 12:49 UTC 没有新建订单。
- C2 的续审提示对真实模型是否有效仍只有替身证据。
- 历史上因这三个缺陷而结束的任务不会自动重审。

## 7. 资金相关说明

- 本次发布不改任何资金开关，不改下单资格逻辑；发布期间双闸保证没有在途提交。
- 发布前只读快照：近 3 小时自动投注真实下单 11 笔（首笔 10:51 UTC，最近 12:37 UTC），其中 9 笔已接单未结算、本金合计 450.00，2 笔已结算。个人策略 `auto_enabled=true`、`emergency_stopped=false`，值未被改动。这些订单始于当日 10:37 UTC 执行器修复之后账户校验恢复，是既有配置恢复生效，不是为验证发起的交易。
- 识别修复会让此前被漏掉的推荐得以录入，自动投注开启时可能因此产生更多订单。

## 8. 回滚（未执行）

1. 撤销迁移登记：`sh deploy-local/compose-base.sh -f .runtime/task78-local-20261111/candidate.yml run --rm --no-deps -T migrate python - < deploy-local/unrecord.py`
2. 恢复镜像：`sh deploy-local/compose-base.sh -f .runtime/task78-local-20261111/rollback.yml up -d --no-deps --no-build --wait api local-mcp-api review-worker web`

回滚目标对三个 API 系服务都是 `sha256:d0f13e9dd4a0…`：review-worker 与 local-mcp-api 各自的旧镜像缺 034，已无法通过结构校验。索引可用迁移文件头部的 `CREATE INDEX CONCURRENTLY` 恢复，旧镜像不依赖它。

发布件位于 `.runtime/ux-hardening/deploy-local/`（不入库）：两个 Dockerfile、构建日志、文件清单、`rollout.sh`、`rollout-web.sh`、`gates.py`、`rollout.log`、发布前后快照与替身演练结果。
