# 账户刷新限流故障修复与执行器发布（2026-11-11）

编号：R6-ACCOUNT-REFRESH-20261111。状态：已实现、隔离验证、**已发布到远端执行器并取得真实自然刷新恢复证据**。

授权来源：用户 2026-11-11 指令“当前乐鱼的账号登录出现重大问题，使用子代理立即使用rea工具分析最新的乐鱼app修复，保障业务系统正常”，以及“批准自动部署到生产环境与自动提交、合并远程仓库，由你具备最高权限”。本次发布未修改个人策略、金额、限额、许可或任何资金开关，未由验证发起任何下注。

## 1. 现象与根因

- `v2_account_jobs kind=refresh` 自 2026-11-10 13:36 UTC 起连续 `failed / upstream_rejected / 0403001`，到发布前累计 1000 余次；此前 5963 次成功。
- 登录没有问题：App 登录续期近 72 小时 5/5 成功（最近 11-11 01:41 UTC），账户始终 `healthy / ready`，余额接口单独调用 HTTP 200。
- 转折点是第二笔 `accepted` 订单进入对账集合：对账 0 或 1 笔时成功，2 笔时失败。执行器 `account_job` 对每笔订单各调一次 `lookup()`，每次都重扫未结与已结历史；第二轮被网关限流（0403001 已由既有真实只读响应核验为“网关限流”）。任务全成全败，余额成功也不上报，校验时间不再推进，页面显示“校验失败”，两笔订单无法对账。

## 2. 乐鱼 App 2.0.3 静态分析（REA）

官方版本接口返回 2.0.3 / 203，`flag="0"`（可选更新）。2.0.3 相对 2.0.2：登录、请求头拦截器、签名配置、6026 新设备验证、订单与余额接口均无逻辑变化，差异只有混淆重命名与编译器重排；路径集合仅新增 3 条充值接口。客户端强制升级判定为 `flag=="1"`，或 `flag=="2"` 且本地版本低于 `history_force_version`。结论：故障不是 App 协议变化造成的。

既有差异（非 2.0.3 新增，本次未改）：`APP_VERSION` 仍为 2.0.1；App 的订单列表走 `yewurecord/…` 路径而我方用 `/yewu13/…`；App 另有按订单号查状态的 `queryOrderStatus` 接口；余额请求 App 带 `cuid/rdm`；preInfo 的 6033。未验证：与 2.0.1 的对比、native 层域名发现解密、场馆侧 `refreshToken` 函数体。

探针范围：经既有受限 SSH 链路在远端采集器容器内执行 1 次内部配置 GET、1 次无凭据 preInfo POST、2 次版本 GET；无登录、无写入。分析产物（含 App 内嵌签名参数的反编译文件）保存在仅 root 可读的 `.runtime` 目录，未进入 Git。

## 3. 修复

| 文件 | 改动 |
| --- | --- |
| `services/executor/account_protocol.py` | 新增 `lookup_many`：同一批（≤2 笔）订单每个历史页只读一次；`lookup` 委托给它。`absence_proves_rejection` 恒为 False。 |
| `services/executor/worker.py` | 一次任务只做一轮历史扫描；`LeyuError`（非 `AuthExpired`）记入 `reconciliation_errors`，不拖累已核验的余额；`AuthExpired` 与未知异常仍让任务失败；最终余额再次成功才上报。 |
| `services/api/app/account_jobs.py` | `order_reconciliation_partial` 且任务成功时给出说明文案（本次未发布到 API，当前无此状态）。 |

生产代码与并行会话留在主检出的未提交补丁逐行相同；本分支新增 7 个回归用例并修正一个夹具顺序问题（策略变更会按既有设计取消仍为 pending 的订单并清零预留本金，夹具需先置 accepted）。两笔订单的历史读取从 4 次降为 2 次（上限 8 → 4）。

## 4. 隔离验证

环境同 `EXPERT_RECOGNITION_FIX_20261111.md` 第 4 节（verifier 镜像、临时 PostgreSQL、CPU/内存受限、内部网络）。

| 标签 | 范围 | 结果 |
| --- | --- | --- |
| r6-a-imported | 13 个账户/订单模块，仅导入补丁 | 228 项 OK，退出 0 |
| r6-b-regress | 同上加新回归用例 | 234 项，1 失败，退出 1：新用例夹具顺序问题（见第 3 节），保留 |
| r6-c-adjacent | `test_identity`、`test_profitable_experts`、`test_verified_source_intent_correction` | 51 项 OK，退出 0 |
| r6-d-final | 13 个模块，夹具修正后 | 234 项 OK，退出 0 |
| r1-acct-preexisting | 未打补丁基线上的账户模块 | 64 项，2 失败，退出 1：`test_history_rate_limit_preserves_fresh_balance_and_other_receipt`（2 != 4）、`test_history_failure_never_masks_final_balance_failure`。两例随 `657e45d5` 先于实现提交 |
| feat-acct-two | 集成分支 `5f3e8536` 上点名跑上述两例 | 2 项 ok，退出 0 |

## 5. 发布（2026-11-11 10:29–10:44 UTC）

1. 只读清单（退出 0，写入 0）：远端 4 个成员运行、重启 0；执行器镜像 `sha256:8070934dd3e9…`（`ai-football-executor:2.0.0-dev-history-2d2a88e5`，源码 `2d2a88e5`，是集成分支祖先）。
2. 源码差异：`2d2a88e5..5f3e8536` 在执行器镜像全部输入内只改了 `executor/account_protocol.py`、`executor/worker.py`，以及执行器不导入的 `collector/leyu_results.py`；Dockerfile 与依赖未变。
3. 构建：从干净的冻结 worktree（`5f3e8536`）用受限构建器（0.5 CPU / 384 MiB）构建 `ai-football-executor:2.0.0-dev-account-5f3e8536`，镜像 ID `sha256:764382631341c4efa4853ba1db9873a873835e293602b3e960f40460d648254b`，用户 `10004:10004`，退出 0。
4. 离线校验（`--network none`，退出 0）：镜像内 27 个源码文件与冻结源码逐一相同；相对线上镜像仅上述两个执行器文件不同。
5. 传输：`docker save` 经既有受限 SSH 链路 `docker load`，远端镜像 ID 与本地一致。
6. 远端预检（0 服务变更，退出 0）：在原 9 文件覆盖链末尾追加本次镜像覆盖后，`compose config` 相对当前只有 `executor.image` 不同；执行器环境变量指纹前后相同，`EXECUTOR_LIVE_ORDERS_ENABLED` 未变；同时写好指向原镜像的回滚覆盖。
7. 部署前快照：在途订单 0、运行中账户任务 0、续期 0；账户 `healthy/ready`，校验年龄 75583 秒；两笔 `accepted` 订单各预留 50.00。
8. 受控切换（退出 0）：10:37:45 取得账户空闲门（账户行锁，运行任务 0、在途 0）；10:37:49 远端仅 `up -d --no-deps --no-build executor`；新容器运行、重启 0、环境/资源/启动参数与旧容器一致、采集器/协议桥/socket-guard 容器 ID 不变、前 25 秒日志无 Traceback；10:38:18 释放门。

### 部署后证据（只读）

| 时间 UTC | 刷新任务 | 计划对账 | 回执 | 结果 |
| --- | --- | --- | --- | --- |
| 10:37:40 | 旧执行器 | 2 | 0 | failed / upstream_rejected / 0403001 |
| 10:38:47 | 新执行器 | 2 | 2 | succeeded，3.5 秒 |
| 10:39:58、10:41:09、10:42:21、10:43:35 | 新执行器 | 0 | 0 | 均 succeeded |

- 账户校验年龄由 75793 秒降到个位数秒并持续刷新。
- 10:38:51 两笔 11-10 的 `accepted` 订单按真实回执对账为 `settled`，预留本金归零（审计 `order_reconciled` 两条）。这是修复的预期结果，不是验证发起的交易。
- 个人策略指纹与绑定指纹部署前后相同，`auto_enabled=true`、`emergency_stopped=false` 未变；在途订单 0。
- 10:44 复查：执行器运行、重启 0、镜像为候选 ID、近 6 分钟日志无 Traceback/error。

### 回滚

远端保留 `rollback-image.json`（指向 `sha256:8070934dd3e9…`）与原覆盖链记录。回滚即按原覆盖链 `up -d --no-deps --no-build executor`，脚本与本次 apply 同源、互为镜像；未执行。

## 6. 未完成与限制

- “部分对账”分支（查单失败但余额成功）只有隔离测试证据；发布后的真实刷新走的是完全成功路径。
- API 的部分对账文案未发布；网页对该状态的展示待前端包核对。
- 是否改用 `yewurecord` 路径或 `queryOrderStatus`、是否把版本号跟到 2.0.3，需先做只读实测，未做。
- 若同一账户同时有 3 笔以上待对账订单，每次任务仍只处理 2 笔（既有上限），其余顺延。
