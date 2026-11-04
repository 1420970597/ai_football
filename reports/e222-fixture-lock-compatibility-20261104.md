# E222 结算比赛锁与审核外键兼容

更新时间：2026-11-04 14:21 UTC。状态：设计已记录，未改线上锁。

目标：结算更新赛果/收益时继续串行保护同场结果，同时允许审核绑定及候选快照引用既有比赛主键。责任目录services/api、tests、reports、docs/platform-v2；依赖E220固定版本及临时Postgres。当前E220完整30分钟冻结，不提前部署。

证据：E219真实resolve binding INSERT锁超时；E220批量后搜索candidate INSERT仍两次LockNotAvailableError，采集接口当时91请求全200。首轮pg等待见transactionid/advisory，后轮未遇；不能据此确定每次锁错都来自结算。代码确认rebuild_settlements和ingest_result取fixture FOR UPDATE，其事务不改变主键id或唯一source_id；该模式会阻塞引用该比赛的外键KEY SHARE。批量仅缩短持锁，不改变锁兼容性。此为可明确复现的冲突机制，实际故障归因继续采样。

决策：仅将上述两处比赛行锁改为FOR NO KEY UPDATE；仍互斥同场结果写入与普通更新，不允许删除或主键改写，不放松signal FOR UPDATE、任务租约、搜索分页、证据或价格校验。无需迁移或数据改写，算法不变。不得把它称完全无锁或已解决所有超时。

实现与验收：先独立临时PG构建真实审核job/8条真实隔离推荐及正式赛果。让rebuild或新版本ingest持比赛锁后，在project阶段通过async barrier暂停，另一连接插入review fixture binding，要求既有800ms锁预算内成功；原FOR UPDATE负向应失败，改后通过，同时并发第二个结果写入仍受互斥保护。保留版本链/收益/时间/幂等和不可变冲突原回归；冻结后定向及完整套件，再等当前窗口结束idle部署。失败恢复原候选、不放宽锁预算；线上样本与隔离机制验证分别报告。
