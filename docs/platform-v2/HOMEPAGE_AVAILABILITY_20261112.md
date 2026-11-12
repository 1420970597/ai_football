# 首页空列表：区分推荐存续、缓存时效与实时询价

## 工作包

- 目标：修复用户报告的首页没有比赛，恢复未结算且来源/比赛阶段/原生盘口结构有效的专家关联，不把缓存年龄直接当作盘口失效。
- 责任目录：services/api/app/routes/、services/api/app/orders.py、tests/、docs/platform-v2/、reports/；主检出infra/production/同步API镜像。
- 前置依赖：源码96246b2d；线上API7dd693d6、web109c6816。已有038结构和独立手动确认保持，不做迁移。
- 当前证据：09:38 UTC真实只读，当天pending足球34/篮球16，来源及阶段有效27/12，五分钟内均0；配置报价15秒，全站fresh_active=0，最新上游报价22秒前。去两道年龄过滤而保留来源/阶段/原生盘口结构后足球3场3条、篮球2场9条。
- 缺口：上轮错误地把推荐五分钟和本地报价15秒同时用于首页展示；手动订单复用自动推荐时效，即使提交前会重新询价仍拒绝旧推荐。
- 设计决策：首页保留pending、未撤回、可信来源、最新消息版本、媒体ready、未完场/未取消、阶段一致、非过期赛事投影、受支持玩法、原生标识、精确盘口/方向及本地active报价结构；去推荐/原文年龄和缓存15秒下限，未知报价时间不冒充新鲜、未来时间仍拒绝。手动明确确认允许较早推荐，但仍绑定相同有效原生盘口，执行器提交前从上游重新询价并校验新鲜预检/单次许可；自动推荐五分钟门与所有账号/会员/限额/来源/回执保护不变。
- 实现动作：局部查询和显式manual资格模式；预留与发送复核一致传递订单来源；添加旧推荐/旧缓存仍可手动而不可自动、原生重新询价、失效分支、分页/摘要与缓存时间的专项回归。仅API部署，不改web/数据库/模型。
- 验收场景：有效未结算比赛恢复；旧缓存不能被当成实际成交价；过旧自动推荐仍拒绝；手动旧推荐经当前上游2.35询价而非1.95推荐价/1.50缓存价提交（仅Mock）；停盘/来源失效/盘口改变仍隐藏或拒绝；生产全程只读验收、无真实资金测试。
- 失败处理：失败保留并定位后重跑原检查；仅回退API镜像，不回滚订单/结构；发现上游停盘等真实无效项不为填充列表而展示。

## 阶段记录

### 2026-11-12 09:41 UTC 设计已记录

Git/当前交接/源查询/交易资格及原生预检既有样本已核对，原四份账户修改保留。docker exec API内default_transaction_read_only=on的两次分层诊断退出0，未发送资金请求。已向用户说明上次过严筛选的错误。效率优先，复用已过的网页/独立买入/038结构证据，仅跑本次资格与相关首页/订单专项。下一步规划提交、两处局部实现、冻结验证及API单角色发布。

### 2026-11-12 09:53 UTC 代码已写，进入冻结隔离验证

两处业务修改：首页不以推荐/缓存年龄清空有效关联；manual标志贯穿预留和最终复核，自动五分钟限制保留。新增旧推荐手动Mock完整执行2.35与自动拒绝、缓存未知/十分钟前仍展示、实际失效条件与分页限额回归。git diff --check退出0。下一步在任务专用Docker临时数据库冻结运行首页、独立确认、报价/未知提交及阶段证据专项；不重跑未变的网页构建或数据库迁移。

首次直接执行infra/test/run.sh退出126（现有脚本无执行位），未启动测试或触碰运行数据；改用sh调用原脚本，不修改权限或测试内容。

### 2026-11-12 09:55 UTC 隔离验证通过

命令：TEST_SUBNET=10.93.88.0/28 sh infra/test/run.sh tests.test_homepage_expert_matches tests.test_manual_buy_intents tests.test_async_score_summaries tests.test_order_stage_evidence tests.test_account_state.AccountStateTests.test_stale_expert_price_reserves_market_and_uses_current_provider_price tests.test_account_state.AccountStateTests.test_unknown_mock_submission_not_requeued_or_released tests.test_account_state.AccountStateTests.test_withdrawn_recommendation_cannot_reserve_money。退出0，39 tests，17.437秒，失败/错误/跳过均0；冻结哈希与报告位于.runtime/test-evidence/2044608/backend-tests.json。旧推荐完整手动Mock接受2.35、自动拒绝，独立确认/网络重放/限额及未知不重试均通过。无真实资金请求。下一步提交推送、仅API不可变薄层镜像、真实只读候选与端口/双端浏览器验收。
