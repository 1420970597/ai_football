# E392 真实足球大小球结算规则修复

更新时间2026-11-06 03:35 UTC。状态：隔离验证通过，待冻结完整回归及真实生效。责任services/api结算策略与投影、tests、reports；完整任务、依赖、验收及失败处理见docs/platform-v2/LEYU_TOTAL_SETTLEMENT_ACCEPTANCE.md。

## 证据与范围

原APK SHA256 298bc7d8844d1530e325d007807c61913d3b764dcc7260caceba919ba49e5462。真实REA普通注单getOrderList/A0和请求DTO、Record类、getOutcome引用链均在固定Docker CLI完成，不从虚拟体育推导规则。普通订单j.i完整smali ev_8a04090737a699dd5a29c83c531b5024e0385c7f11a91894adbd67da933a78ce与j.j完整Java ev_687dd13af8362dea10fa4cc7b09c4df97a4c052d99f921fc4f07a363096293e7确认outcome2走盘/3输/4赢/5半赢/6半输。

已有授权会话同端点单页50条普通历史单：首次仅字段清点200/1187.923ms，随后为取得最小证据再次单页读取200/1976.407ms，响应SHA c9b93ae8969cdbb617183fbad83a5c62fbe60107a3e7ae0a15d74262364c9829。既有会话、原base/relay，不登录续期、不下注、不打印或保存账号订单标识/金额/凭据。第二次只保留足球sport1的玩法编号、期间文本、盘口、比分和结果码。50条中16项为play2/18；1项settleScore为空排除，其余15项结果全部与所属期间累计总进球吻合。5项能区分扣除基线与不扣除，实际均支持不扣除：

| 玩法 | 方向/线 | 投注比分 | 期间终场比分 | 官方普通单结果 | 整期/剩余期 |
| --- | --- | --- | --- | --- | --- |
| 全场2 | 大2.25 | 1:0 | 2:0 | 6半输 | 半输/全输 |
| 全场2 | 大3.25 | 1:0 | 2:1 | 6半输 | 半输/全输 |
| 全场2 | 大2.25 | 0:1 | 1:1 | 6半输 | 半输/全输 |
| 上半场18 | 大1.5 | 0:1 | 0:2 | 4全赢 | 全赢/全输 |
| 上半场18 | 小1.5 | 0:1 | 0:2 | 3全输 | 全输/全赢 |

这是提供方真实普通结算行为的限定规则依据，不声称拿到全部玩法官方条款。不能扩展到让球、第二节/半场、篮球或其它提供方。研究比较中的1.90是仅用于纯函数结果分类的模拟价格，不是上述真实订单赔率，未计算或展示真实账户收益。

## 实现与验证

局部纯规则选择器只接受leyu:ASCII数字、football、live、total、原始basis unknown、market2/full_time或18/first_half。仅原纯结算返回live_market_score_rule_unknown才按full_period再算，已有零基线/双口径等价正确结果继续原reason与日期。算法v6新增不可变计算行；原signal输入保留unknown/比分/价格，另存score_rule引用及effective_signal，不改报价、旧台账、原文或实盘订单。

Docker限额临时PostgreSQL原整组60项首轮51.014秒actual1，唯一新测试多context一元+夹具TypeError；修正后60项49.996秒actual0。上线前审查进一步保留既有已结算日期，增加三天前收益重建不换日期行为后最终61项50.635秒actual0，0失败/错误/跳过；包括四纯规则及原生五分歧样本、旧pending及unknown快照原样保留、新行幂等、半场大/小、缺半场比分、结果修订、坏报价拒绝和历史日期保持。运行时冻结被测源、未改线上。

实际命令bash .runtime/e209-live/e392-targeted-tests.sh，内部唯一具名ai-football-e392-targeted-pg临时库与受限Verifier，退出后只清理本任务容器/网络。独立无网e392-native-evidence-check.py实际0确认15有效、1缺证、5分歧和全期结果全吻合。git diff --check实际0。下一步提交冻结源码、完整1515预期套件、候选精确核验，自然空闲门单API部署与有界重建，双端真实点击及新整窗；当前不得称已修复上线。
