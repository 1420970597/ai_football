# 专家发言识别缺陷修复（2026-11-11）

编号：WPC-EXPERT-RECOGNITION-20261111。分支 `feat/TASK-78-ux-backend-expert-hardening`，隔离工作树 `.runtime/ux-hardening-worktree`（主检出同时有另一会话在修 BUG-EXPERT-RESULT-20261111，本项不触碰其未提交文件）。

状态口径：已实现、隔离验证，并于 2026-11-11 12:43 UTC 发布到生产（见 `LOCAL_RELEASE_20261111.md`）。未做真实模型联调、未重审历史消息；这些在文末“未验收与后续”列出。

## 1. 分析方法

只读查询生产库（`ai-football-v2-api-1` 容器内，只读事务 + `statement_timeout=8s`，不输出任何连接信息或凭据），以审核轨迹 `v2_analysis_traces` 为准，不看模型自述下结论。

48 小时窗口内事实（2026-11-11 07:10–08:20 UTC 查询）：

| 项 | 数量 |
| --- | --- |
| `system_review_result` unresolved | 1290 |
| 其中“比赛已唯一识别但无信号”（`identified_without_signal` 非空） | 98 |
| `system_review_result` succeeded | 148 |
| `review_failure` provider_transient（HermesProviderUnavailable） | 212 |
| 读盘刷新回执 `upstream_cooldown` | 26 次 / 10 个任务 |

对 98 条“已识别无信号”和 12 条“检索过但未绑定”逐条读轨迹后归类：

| 归类 | 性质 | 处理 |
| --- | --- | --- |
| C1 上游冷却期内读盘，报价全部不可确认，任务直接结束 | 缺陷 | 本次修复 |
| C2 模型把最后一次工具调用写成正文，未执行即结束 | 缺陷 | 本次修复 |
| C3 来源“中文队名 + 空格 + 年龄组”检索零候选 | 缺陷 | 本次修复 |
| 盘口已 closed/suspended | 正确拒绝 | 不改 |
| “破蛋”“0.75大”等未注明期间 | 既有证据策略 | 不放宽 |
| 篮球单节盘（一节至四节，`unsupported_market_kind`） | 功能缺口 | 记录，未做 |
| 总进球区间“2-3”、单双、篮球独赢无库存 | 功能缺口/无库存 | 记录，未做 |
| 赛果反馈、广告图 | 正确判为无推荐 | 不改 |

## 2. 缺陷与根因

### C1 上游冷却导致明确推荐被丢弃

样本：任务 `7b99412b…`（纯文本三条日联杯推荐：横滨水手 +0.5、町田泽维亚 +0/0.5、总进球 2-3）、`f02e4024…`（“南亚杯 17:30 印度 -2.25”）。三场比赛、目标盘口均已找到（如印度 -2.25 的报价存在且 active），但 `mcp_list_match_markets` 回执为 `refresh={attempted:true,accepted:false,reason:"upstream_cooldown"}`，所有选项 `fresh=false / confirmed_current=false / can_record=false`。模型按规则不提交，任务以 unresolved 结束，`attempts=1`，没有 `market_recovery_scheduled`。

根因：

1. `services/api/app/review_recovery.py` 的 `TRANSIENT_REFRESH_REASONS` 只含 `refresh_timeout/refresh_unavailable/refresh_busy/refresh_throttled/ingestion_failed/refresh_failed`，采集器共享退避产生的 `upstream_cooldown`、`upstream_rate_limited`、`upstream_timeout` 不在其中，既有测试还把 `upstream_rate_limited` 断言为“永久失败不重试”。
2. 采集器 `services/collector/leyu_refresh.py` 在回执里给了 `retry_after_seconds`，但 API `_request_market_refresh` 丢弃了它，系统无从知道该等多久。

同一 48 小时内，10 个遇到 `upstream_cooldown` 的任务里 6 个以 unresolved 结束。

### C2 未执行的工具调用被当作审核结论

样本：任务 `a17d1748…`（“埃及电信+1 阿拉维斯大4.5 埃瓦尔-2 斯托克港 -1.5”）。三场已绑定、三个报价 `can_record=true`，模型最终输出是英文推演并以 `Let me do batch A.` 加一个工具调用闭合标记结尾，`finish_reason=stop`，`mcp_record_expert_signals` 调用数为 0，`completion_verified=true`。即提交动作只被序列化成了文字，没有执行。

根因：完成性校验（`review_completion.py`）只核对分页、绑定、阶段/盘口说明冲突和“声称无工具”，不识别“输出停在工具调用标记上”。全量历史 1916 条 `hermes_result` 中命中 1 条；频率低，但每次都是整条消息的全部推荐丢失且无任何告警。

### C3 中英混排空格导致检索零候选

样本：任务 `13fb9500…`、`1bfbe485…` 用“雪兰莪 U20 / 达罗塔晋C队 U20”检索为 0 候选；任务 `ae37a29f…` 用“雪兰莪U20”命中 1 条（库内队名 `雪兰莪U20`）。同一场比赛，只差一个排版空格。

根因：`review_candidates.snapshot_candidates` 的 SQL 把库内队名的非字母数字折叠成空格后做 `LIKE`，查询词“雪兰莪 u20”中的空格无法匹配库内连续写法。身份比较侧 `team_matching._name_forms` 早已处理中文边界空格，召回侧没有。库内 604 支队名是“中文紧接字母数字”写法，带空格写法只有 3 支，来源图片/正文带空格很常见。

## 3. 修复

| 缺陷 | 文件 | 改动 |
| --- | --- | --- |
| C1 | `services/api/app/routes/reviews.py` | `_request_market_refresh` 在刷新未被接受且采集器给出 1..900 的整数 `retry_after_seconds` 时保留该字段；只写入系统轨迹，返回给模型的 `refresh` 仍只有 `attempted/accepted/reason`。 |
| C1 | `services/api/app/review_recovery.py` | 新增 `UPSTREAM_BACKOFF_REASONS=(upstream_cooldown, upstream_rate_limited)`，`upstream_timeout` 归入瞬时原因；延迟由 `recovery_delay_seconds` 计算：普通瞬时 20 秒，上游退避取 `clamp(提示, 60, 900)+5` 秒。仍受 `MARKET_RECOVERY_ATTEMPTS=2` 约束（只多审一次）。 |
| C2 | `services/api/app/review_completion.py` | 新增 `unexecuted_tool_call_claim`（只看输出末尾 400 字符是否以工具调用闭合标记收尾）、`unexecuted_tool_call_issues`、续审提示。 |
| C2 | `services/api/app/worker.py`、`review_summary.py` | 实时审核命中时走既有续审回路（最多 3 轮）；最终系统回执独立复核，三轮仍如此则 `completion_verified=false` 并写明原因。 |
| C3 | `services/api/app/team_matching.py`、`review_candidates.py` | 抽出 `compact_chinese_boundaries`，召回模式追加“去掉中文边界空格”的写法。 |

没有放宽的东西：

- 报价是否可记录仍只由 `fresh_quote` 或当前原盘复核决定；冷却回执不会让任何报价变成可记录，录入时仍会返回 `quote_inactive_unknown_or_stale`。
- 续审提示明确写明不是新的绑定、盘口或录入许可。
- C3 只扩大候选召回，绑定仍由 `resolve_match` 对双方完整身份（含年龄组）比较。

## 4. 隔离验证

环境：`ai-football-verifier:v2.0.0-dev`，`--cpus=.75 --memory=512m`；临时 PostgreSQL `postgres:16-alpine`（tmpfs，`--cpus=.5 --memory=384m`，容器名 `ai-football-v2-uxh-pg-<pid>`），内部网络固定子网 `10.93.77.0/28`，源码只读挂载，结束即删除。无生产连接、无真实模型、无资金。

首次建网失败记录（保留）：`docker network create --internal` 退出 1，`all predefined address pools have been fully subnetted`（主机已有 86 个网络）；改为显式子网后通过。

| 步骤 | 目标 | 结果 |
| --- | --- | --- |
| C3 红灯 | 去掉修复只留新测试，`tests.test_search_resolution` | 15 项，1 失败（`['fixture-1'] != []`），退出 1 |
| C3 绿灯 | `tests.test_search_resolution tests.test_chinese_names` | 20 项通过，退出 0 |
| C1 第一次 | `tests.test_review_worker tests.test_reviews` | 161 项，6 错误，退出 1：新用例在子用例间未把任务重新置为可领取（测试自身问题） |
| C1 第二次 | 同上 | 161 项，1 失败，退出 1：字符串形式的 `"137"` 被 SQL 当成有效提示（实现缺陷，已改为要求 JSON number） |
| C1 第三次 | 同上 | 161 项通过，退出 0 |
| C2 | `tests.test_review_tool_availability` | 22 项通过，退出 0 |

审核链路相关模块合并回归结果见第 5 节。

## 5. 合并回归

`run_tests.sh wpc-review-subset`：`tests/test_review*.py`、`tests/test_search*.py`、`tests/test_chinese_names.py`、`tests/test_hermes_client.py`、`tests/test_team*.py` 共 476 项，363.859 秒，0 失败/错误，退出 0（2026-11-11 08:20 UTC，工作树基线 `085c80f1` 加本次未提交改动）。

未跑完整冻结套件（2312 项）与浏览器套件：本次改动只涉及 API 审核链路模块，完整套件留到本分支其余工作包合并前一起跑。

## 6. 未验收与后续

- 已部署（2026-11-11 12:43 UTC）：api、review-worker、local-mcp-api 统一切换到含本修复的镜像，过程与证据见 `LOCAL_RELEASE_20261111.md`。发布后尚无一条真实带推荐的审核走过新逻辑的证据。
- 未做真实模型联调：C2 的续审提示是否能让真实模型补上提交，只有合成模型替身验证。
- 未重审历史：`7b99412b…`、`f02e4024…`、`a17d1748…` 等已结束任务不会自动恢复。
- C2 未覆盖“以自然语言宣称要调用却停下”的情形，只识别明确的调用标记收尾，避免把正常结论误判为未完成。
- 篮球单节盘（一节至四节）当前是 `unsupported_market_kind`，30 小时内至少 9 条推荐因此无法录入，属功能缺口，需单独立项。
- Hermes 上游 `provider_transient` 48 小时 212 次，属供应商稳定性，不在本次代码修复范围。
