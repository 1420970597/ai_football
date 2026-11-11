# 系统重构计划（效率优先，2026-11-11）

编号：REFACTOR-20261111。授权来源：用户 2026-11-11 指令“在分析后指定重构计划，指派多个子代理完成系统全面重构，以效率优先（开发效率+运行时效率）”，以及同日紧急指令“乐鱼的账号登录出现重大问题，使用子代理立即使用rea工具分析最新的乐鱼app修复，保障业务系统正常”。多代理协作因此获得明确授权（AGENTS.md 二.2）。

集成分支 `feat/TASK-78-ux-backend-expert-hardening`（基线 `eddc8685`）。每个工作包一个独立 worktree 与分支 `refactor/TASK-78-<包名>`，位于 `.runtime/refactor-wt/<包名>`。主检出上另一会话的未提交改动不触碰。

## 1. 分析结论（均为 2026-11-11 实测）

### 规模与热点

| 区域 | 规模 | 21 天内提交触及次数（全库 1351 次提交） |
| --- | --- | --- |
| `services/api/app/routes/reviews.py` | 663 行 | 65 |
| `integrations/hermes/skills/expert-review/SKILL.md` | 单文件规则 | 48 |
| `services/collector/telegram.py` | 1741 行 | 46 |
| `services/api/app/worker.py` | 724 行 | 46 |
| `services/api/app/hermes_client.py` | 422 行 | 37 |
| `web/app/app.js` | 2056 行，36 行超过 200 字符 | 36 |
| `services/api/app/routes/telegram.py` | 1231 行 | 32 |
| `tests/` | 199 个文件 40954 行 | — |

### 运行时

| 观测 | 数值 | 来源 |
| --- | --- | --- |
| 审核模型输入 | 平均 40983 tokens，p50 16711，p90 74535；平均 19.7 秒，p90 51.8 秒 | 48h `hermes_result` 1536 条 |
| `mcp_list_match_markets` | 平均 4.6 秒，p95 9.4 秒 | 48h 570 次 |
| `mcp_record_expert_signals` | 平均 5.0 秒，p95 13.1 秒 | 48h 249 次 |
| `mcp_search_matches` 响应 | 平均 7.9 KB，最大 77 KB | 48h 744 次 |
| `v2_current_quotes` | 2554 MB（堆 1153 MB + 索引 1400 MB），161 万行，其中已完场 120 万行；3 天留存已生效 | `pg_stat_user_tables` |
| 账户刷新任务 | 11-10 13:36 UTC 起 932 次全部 `upstream_rejected/0403001` | `v2_account_jobs` |
| 静态资源 | `app.js` 103900 B 等全部未压缩（已在 `eddc8685` 修复，未部署） | curl 生产 |
| 首屏请求 | `/auth/me`、`/admin/me` 重复 2–3 次，`/public/experts`、`/public/signals` 重复 2 次 | 启动序列读码 + 生产抓取 |

### 开发效率

| 观测 | 数值 |
| --- | --- |
| 完整冻结套件 | 2312 项 969 秒；23 个测试文件每个用例都 `CREATE DATABASE` 并顺序应用 33 个迁移 |
| 测试网络 | Docker 默认地址池已满，`infra/test/run.sh` 与 `tests/browser/check.sh` 建网直接失败 |
| 主机 | 4 核 / 8 G，swap 用 10 G；本项目遗留运行中测试容器约 70 个占约 3.6 GiB，已退出容器约 1400 个 |

### 紧急项定性

登录本身没有坏：App 登录续期 72 小时内 5/5 成功（最近 11-11 01:41 UTC），账户 `healthy/ready`，采集持续写入。故障是账户刷新任务在需要对账 2 笔订单时逐笔重复扫描订单历史，触发网关限流 `0403001`，而任务是全成全败，余额校验时间不再推进，页面显示“校验失败”，两笔已接单订单无法对账。转折点与第二笔订单进入对账集合的时间一致（对账 0/1 笔成功，2 笔失败）。App 协议是否另有变化由 REA 子代理独立核对。

## 2. 工作包

文件归属互斥；表中未列出的文件不归任何包，需要改动时回到集成者决定。

| 包 | 归属文件 | 目标 | 门禁 |
| --- | --- | --- | --- |
| R6 account（最高优先） | `services/executor/**`、`services/api/app/account_jobs.py`、`orders.py`、`routes/accounts.py`、`services/collector/leyu_session.py`、`tests/test_account*.py`、`tests/test_manual_betting*.py`、`tests/test_leyu_session.py` | 同账户多订单共用一次有界历史扫描；对账失败不拖累有效余额；失败订单保留未知状态与敞口；按 REA 结论修正协议差异 | 账户/订单相关测试全绿；不触碰真实资金开关；部署单独审批 |
| R1 tests-speed | `infra/test/**`、新增 `tests/dbsupport.py`、各测试文件中仅 `asyncSetUp/asyncTearDown` 的建库段、`tests/browser/check.sh` | 模板库替代逐用例迁移；显式子网；可按模块运行 | 完整套件项数不变、0 失败、耗时下降并给出前后数字 |
| R2 web | `web/app/**`、`tests/browser/**`（除 `check.sh`）、`tests/web/**` | 首屏去重请求、管理端与二维码脚本按需加载；首页/专家/账户页与手机端布局整理（保持捷报比分参照）；一键下注面板体验 | 浏览器 67 项及其余脚本全绿；视觉基线变更逐张说明 |
| R3 review | `services/api/app/worker.py`、`routes/reviews.py`、`review_*.py`、`hermes_client.py`、`services/hermes-mcp/**`、`tests/test_review*.py`、`tests/test_search*.py`、`tests/test_hermes*.py` | 降低读盘/录入往返与工具响应体积；拆分高频改动文件的职责；不改提示词语义 | 审核链路 476 项全绿；每项优化给出前后往返数或字节数 |
| R5 db-runtime | `services/api/app/routes/public.py`、`ingestion.py`、`raw_retention.py`、`database.py`、`services/api/migrations/035+`、`infra/compose*.yml`、`infra/` 下新增清理脚本、对应测试 | `v2_current_quotes` 行宽与索引、公开接口查询、连接池；遗留容器清单与 dry-run 清理脚本 | 相关测试全绿；迁移可回滚；不执行任何生产删除 |
| R4 collector-telegram | `services/collector/telegram*.py`、`services/collector/worker.py`、`services/api/app/routes/telegram.py`、`tests/test_telegram*.py`、`tests/test_collector*.py` | 先度量再动：按内聚边界拆分两个千行文件，消除重复往返 | Telegram/采集相关测试全绿；无行为变化 |

## 3. 执行规则

- 共同规则见 `.runtime/ux-hardening/AGENT_RULES.md`：只在自己的 worktree 改归属文件；一切执行在 Docker；测试经 `run_tests.sh` / `run_browser.sh` 全主机串行（共用一把锁）；不部署、不 push、不碰生产写入与真实资金。
- 行为保持：对外接口、数据库语义、审核证据与资金规则不变，除非表中点名。
- 合并顺序：R6 → R1 → 其余按完成先后；每次合并后在集成分支重跑受影响模块，全部合并后跑完整冻结套件与浏览器套件。
- 与另一会话的协调：其分支 `fix/TASK-78-hermes-telegram-sync` 仍在推进。本计划避免无度量依据的文件搬移；R6 以其未提交补丁为起点并注明来源，合并前再对齐一次。

## 4. 不在本计划内

- 生产部署（尤其远端执行器，真实下单路径）需单独的配置差异检查、回滚方案与用户确认。
- 停止或删除遗留容器：只交付清单与 dry-run 脚本，执行由用户确认。
- 篮球单节盘口支持、Hermes 供应商稳定性（48h `provider_transient` 212 次）。
- 提示词/技能文本语义调整：没有真实模型回归手段前不动。

## 5. 状态

2026-11-11 09:00 UTC：计划入库，worktree 已建，子代理派发中。各包结果、命令与退出码在完成后追加到本节。

### R6 account（已合并、已发布）

合并提交 `5f3e8536`。13 个账户/订单模块 234 项通过；远端执行器 10:37 UTC 切换为 `ai-football-executor:2.0.0-dev-account-5f3e8536`，10:38:47 起自然刷新恢复成功，两笔滞留订单完成对账。命令、退出码与回滚见 `ACCOUNT_REFRESH_FIX_20261111.md`。

### R3 review（度量后结论：不改代码）

逐调用点核对后，同一工具调用内的 `active_job` 重复不能安全合并：轨迹锁按任务独占并持有到事务结束，提前获取会把同一任务的并行工具调用串行化；`tool_success` 的取锁必须是写入前的独立语句，否则会撞轨迹序号唯一约束；刷新 I/O 之后与新事务里的重新校验是安全校验，必须保留。唯一真正的重复（录入不刷新路径）只省 1 次往返，却会让已失效的尝试先取写锁再回滚，未做。语句数断言 8 / 6 / 9 不变，零提交。

生产 48 小时按刷新结果拆分 `mcp_list_match_markets` 耗时：触发并完成上游刷新（`written`）433 次，平均 5278 ms、p50 4504 ms；未触发刷新的各类（冷却、筛选无报价、已新鲜等）p50 在 24–632 ms。即读盘 4.6 秒的均值几乎全部是对上游的精确刷新 I/O，不是数据库往返；继续优化要从“何时需要刷新”入手，属于审核时效与证据新鲜度的取舍，不在本次行为保持型重构内。


### R5 db-runtime（已合并 `f82a609a`，未发布）

- 迁移 `035_drop_redundant_active_quote_index.sql`：删除 `v2_current_quotes_active_idx`。它与覆盖全部状态的 `v2_current_quotes_fixture_fresh_idx` 键相同，生产 `idx_scan=0`（5608 kB），每次活跃报价更新都白白维护。迁移先断言覆盖索引存在且有效。回滚 SQL 写在迁移头部。
- `/public/signals`：一次请求内会话只解析一次，数据库往返 6 → 4。
- `infra/quality/docker-leftovers-inventory.sh`（只读清单）与 `docker-leftovers-cleanup.sh`（默认 dry-run，`--execute` 才删除；保护 `ai-football-v2[-_]*` 与构建器）。
- 合并结果验证：`feat-r5-merge` 11 个模块 140 项，退出 0。
- 发布约束：`check_schema` 精确比对迁移集合，含 035 的镜像要求 api、review-worker、local-mcp-api 同批切换。
- 未实现、留作后续：535 MB 的 `v2_quotes_fixture_summary_idx` 膨胀度量后 `REINDEX CONCURRENTLY`；已完场报价更早回收（改变留存语义）；连接池上限之和（48+24+8）等于 `max_connections=80`，实测在用 13–18 条，未改。

### R1 tests-speed（已合并 `df652323`）

- `tests/dbsupport.py`：每个用例从“已迁移模板库”克隆，不再逐用例顺序应用全部迁移；模板名带迁移集合摘要与创建角色，迁移变化或测试打补丁换旧结构时不会复用错模板。
- `infra/test/run.sh`：显式子网（默认地址池已满）、依赖未变时复用镜像、可按模块运行、可选全主机串行锁、最后一行 `exit=N` 且作为退出码；`tests/browser/check.sh` 同样改用显式子网。
- 结果：完整冻结套件 2342 项 495.4 秒，退出 0（基线 2312 项 969 秒；多出的 30 项是本分支新增用例）。三模块对比 199.7 / 182.8 秒 → 64.8 秒。
- 保留的失败记录：首次完整套件未运行（直接执行 `infra/test/run.sh`，文件模式 100644，`Permission denied`，0 项）；改 `sh infra/test/run.sh` 后通过。未打补丁基线上两条账户用例失败属 R6 修复前的既有状态，见 `ACCOUNT_REFRESH_FIX_20261111.md`。

### R4 collector-telegram（纯搬移已合并 `2799abe5`；其余为度量结论）

- 已做：6 个无状态辅助定义移入 `services/collector/telegram_common.py`（50 行），`telegram.py` 1741 → 1697 行。搬移后 `r4-move` 344 项退出 0；搬移前基线 `r4-base` 是另一组模块（510 项退出 0），两次范围不同，不能直接对比。补跑 `r4-move-gap` 166 项退出 0。合并结果上 `feat-r4-merge` 跑全部 `tests/test_telegram*.py` 与 `tests/test_collector*.py` 共 19 个模块，344 项退出 0。
- 不再拆的原因：测试对 `collector.telegram.<常量>` 打了十余个名字的补丁（按 `patch('collector.telegram.大写名'` 检索为 16 个），搬走后补丁落在重导出副本上静默失效；最长的几个函数是带租约、幂等和可信作者判定的单事务流程，拆分即改业务路径。
- 度量发现，未改：`media_orphans`（`routes/telegram.py:945-979`）每轮同步（约 10 秒）调用一次，每次三次顺序扫描，而孤儿阈值是 24 小时；管理端消息列表（`routes/telegram.py:1163-1168`）缺 `(posted_at DESC, id DESC)` 索引。前者适合节流到分钟级并加部分索引，属行为节奏变更，另行决定。
