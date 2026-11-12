# 首页每次明确确认独立买入（2026-11-12）

## 工作包

- 目标：用户明确要求首页买入不因同推荐有历史订单而返回旧accepted；每次新的明确确认应创建独立买入意图。
- 责任目录：services/api/app/、services/api/migrations/、web/app/、tests/、docs/platform-v2/、reports/，主检出infra/production/固定上线产物。
- 前置依赖：80480f49首页筛选及当前赔率已上线；API实际orders/routes模块需逐文件核对，保留主检出他人账户修改。
- 当前证据：reserve_signal按user+signal直接返回任意历史订单，固定expert:user:signal幂等键，数据库还有user+signal唯一索引；这同时阻止人工再次买入并把历史状态误当新结果。前端仅发送signal_id。
- 缺口：没有区分新的明确确认与同一次网络重发；用户报告accepted未实际买入，当前没有足够上游事实证明此历史状态，应保留证据而非编造成交或清除旧订单。
- 设计决策：手动买入使用每次确认的新request_id，服务端以user+request_id幂等；不同确认同推荐允许多单，自动推荐去重规则保持。相同request_id跨推荐拒绝；账号锁、权益、金额/日限额/敞口、未知回执单次提交继续生效。缺少request_id的旧客户端明确提示刷新，不能生成不可恢复的隐式重复请求。索引只收窄到自动订单，保留请求幂等唯一约束，不删除历史订单。
- 实现动作：增量迁移、独立请求键及前端确认发送；正常新买入返回真实新预留订单，历史同一次请求重放用明确文案，不声称已新成交。隔离测试多次确认、accepted旧单、同请求重复/并发/跨推荐、自动去重、金额敞口及权限；协议Mock验证继续按当前赔率。
- 验收场景：同推荐两次确认得到不同订单；同确认重发只占一份本金；旧accepted不再代替新请求；越权及超限仍拒绝；不执行真实资金测试。
- 失败处理：保留原检查/退出码，定位后重跑，不跳过安全门。迁移前备份并在隔离库恢复验证，只执行有锁增量；由于现有check_schema精确匹配迁移列表，API/review-worker/local-mcp-api要携带新迁移清单，其他无该检查的角色不重建。回退应用不得删除新订单或回滚原有业务数据。

## 阶段记录

### 2026-11-12 08:33 UTC 设计已记录

用户补充为明确新增授权，继续执行。现有唯一索引不能仅靠前端或去掉一次SELECT解决；必须有序迁移。已确认只有API、review-worker和local-mcp-api启动调用精确check_schema；collector、raw-retention不依赖该清单，executor仅走接口。当前研究/设计完成，下一步规划提交、局部实现与专项冻结验证；历史accepted真实性保持待核验，不修改历史状态或上游订单。

### 2026-11-12 08:39 UTC 隔离验证通过

新增038仅把原user+signal唯一索引收窄到自动订单，原请求键唯一约束保留；手动POST必填request_id。每次前端确认使用既有newId生成新编号，只发送推荐及编号，不发送赔率。reserve_signal保留本人账户事务锁/权限/金额及限额，手动同请求重放校验原推荐和账户绑定后返回同单；不同确认不查询旧推荐订单。

TEST_SUBNET=10.93.85.0/28 sh infra/test/run.sh tests.test_manual_buy_intents tests.test_migrations tests.test_manual_betting_limits tests.test_account_state.AccountStateTests.test_manual_bet_uses_own_saved_stake_without_auto_betting_authorization tests.test_account_state.AccountStateTests.test_manual_bet_respects_limits_and_membership tests.test_account_state.AccountStateTests.test_manual_bet_rearm_requires_explicit_confirmation tests.test_account_state.AccountStateTests.test_unknown_mock_submission_not_requeued_or_released tests.test_account_state.AccountStateTests.test_stale_expert_price_reserves_market_and_uses_current_provider_price tests.test_homepage_expert_matches退出0：40项、19.862秒，0失败/错误/跳过，冻结证据.runtime/test-evidence/1872596。六项新增含并发同编号一次预留、不同确认2单及第3单超限、旧accepted保留、当前报价、跨推荐/绑定拒绝、自动去重与037增量重复执行。当前准备源码提交、实际镜像浏览器、备份恢复及受控迁移；尚未发布本工作包。证据目录复制首次用错相对工作树路径退出1，改绝对已纠正，未改生产。
