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

### 2026-11-12 08:46 UTC 实际候选及订单恢复演练通过

源码3049c0503e51d45bf76eb43d082c80e0358944b2已推送并远端核对。精确原API仅两业务模块及038变化，review-worker/local-mcp-api仅补038清单以满足启动检查；全/app摘要及三文件冻结SHA逐一相同，运行用户10001:10001保留，Compose只变四角色镜像。网页使用同一提交及既有资源版本构建。实际nginx三个视口和买入20项退出0，包含同推荐再次确认使用不同UUID且无历史订单挡板。

运行角色DDL权限初验退出1，因为误将运行角色当迁移角色；保持运行角色无DDL，复用受保护nov11-local-db/admin.env管理引用，TLS仍verify-full，只把DATABASE_URL传入短命管理进程，未提升应用权限。真实只读预检0：已登记36个迁移文件（序号到037，中间有缺号），唯一待应用038；现有42单均manual=false，其中accepted4单有上游订单引用。不能把这些历史状态说成本次手动买入，亦不擅自判定历史上游回执为假。

pg_dump仅备份受本次结构变更影响的v2_bet_orders及v2_schema_migrations，退出0，22KiB，SHA d21ff77c0943c7ad32c06e5de0ae530fc45756f53fdaca4bea9f204ad295fb5c。Docker独立内网/临时PG恢复真实备份并还原旧索引后，应用038、重复应用及check_schema退出0；42条原订单逐行摘要95811c5bd985671be8d8f4cca745f835前后一致。此为订单表/迁移索引恢复演练，不冒充整平台媒体恢复。证据主检出.runtime/manual-buy-intents-release-20261112/，受保护目录不入Git。当前准备独立有界审核门、显式有锁迁移及四角色更新；未执行真实交易。

### 2026-11-12 08:50 UTC 迁移成功，MCP启动验收失败并补齐清单

独立短命容器有界门08:47:05 UTC取得、随后正常释放，未随worker更新丢失门连接。038受控迁移退出0：仅索引及迁移登记变更，事务内锁表核对42条订单摘要4771a2288476d58431c16d5e49fb0f5c前后相同。备份到迁移之间后台可正常更新业务，故不将备份时摘要当迁移前摘要。首次四角色Compose退出1：API/web healthy、worker running，local-mcp-api因MigrationRequired重启，发布验收仍未通过。

真实MCP角色只读对比定位：原8088087e镜像缺少已在库应用的037，原进程健康不能证明重启兼容；首次候选只补038而未对每个角色验证完整清单。修复仅把同源、与已登记checksum一致的037加进MCP镜像，绝不重执行037、不放宽check_schema。单角色候选用实际MCP配置只读check_schema通过后，仅更新local-mcp-api并重跑原健康/配置验收；应用代码、原订单、权限及其他角色不再改动。失败日志和首轮镜像保留于受保护证据目录。

### 2026-11-12 08:54 UTC 发布验收通过

MCP完整清单只读check_schema通过，单角色重部署退出0。最终API7dd693d6…、web109c6816…、review-worker0716a73c…、local-mcp-apia88e2b10…均同源码3049c050/版本2.0.0-dev-manual-intents.20261112.1；API/web/MCP healthy、worker running，各RestartCount0，环境/用户/命令/挂载/资源/端口/健康/安全属性与原值相同，其他6个同项目服务ID/镜像不变。全/app差异复核：API仅orders、accounts与038；worker仅038；MCP仅037/038目录补齐。网页实际静态与提交归档一致。

实际三个角色逐一使用原数据库身份、只读及TLS检查完整迁移校验通过，API/MCP readyz返回ready。3003/公网HTML和9项运行资源分别与镜像逐字节一致，4个双运动API检查、1440/390双运动4个只读浏览器均退出0，0页面异常/溢出/写请求。实际资金验收未执行，生产没有写测试订单，已有订单状态未清除或重分类；新请求/重复确认的业务行为由40项隔离及20项真实网页镜像交互覆盖。当前没有合格推荐时显示真实空列表，不编造演示场次。

证据reports/manual-buy-intents-20261112.json已Docker封存0，包含恢复/迁移事务摘要、完整角色结构检查、原失败及修复边界。回退限制：038应用后新手动订单可重复推荐，不能通过删除订单恢复旧唯一索引；旧应用若需临时恢复必须携带完整038迁移清单，优先向前修复。下一步仅封存提交推送及主检出生产覆盖/交接同步，不再改已验收应用代码。
