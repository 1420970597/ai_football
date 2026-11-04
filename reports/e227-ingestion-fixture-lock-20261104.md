# E227 赛事采集比赛锁兼容

更新时间：2026-11-04 15:15 UTC。状态：设计已记录，未改线上。

目标：采集更新比赛/盘口时继续串行保护同场事实，但允许审核候选和绑定引用既有比赛，补足E222仅覆盖赛果锁的缺口。责任目录services/api、tests、reports、docs/platform-v2；前置E225冻结版本、临时PG、当前15:20:17完整冻结窗。证据：E222仍有search/collector锁问题；源码ingest_fixture_batch在upsert后SELECT stored显式FOR UPDATE，会阻塞审核外键KEY SHARE。其更新列均不改变id及唯一source_id。E222等待采样另见review_lease_renewal被其他语句挡住，不据此把全部超时归因于本锁。

决策：只将该SELECT锁改为FOR NO KEY UPDATE，与结果写入仍互斥，保留单调时间、完场保护、完整盘口关闭、报价版本、source_id顺序及整个事务。原upsert不写id/source_id，不能放松正在改主键的事务。无需迁移、不改租约/价格/权限/超时。E226报价批量读取保持研究，优先修这处机制明确的阻塞。

验收：真实临时PG ingest_fixture_batch更新时，在后续盘口写入前通过连接代理barrier暂停，另一连接在800ms原预算内插入该已存在比赛的review fixture binding；旧FOR UPDATE负向应锁错，候选通过，同时第二个FOR NO KEY UPDATE写者100ms仍必须锁错。再继续原事实与盘口提交，核验数据完整。冻结后定向及完整套件；当前窗口先保存终审，再等待候选全套通过后idle一次更新API/worker/MCP到包含E223/E225/E227的同一源，避免接连重启，不混入E226研究。失败不加timeout或绕过外键，保留旧证据。真实改善仍须下一完整30分钟证明。

15:17负向已复现及候选冻结：原实现真实PG用例1项2.342秒/1failure/exit1，binding为LockNotAvailableError，第二写者100ms仍受阻。独立aee11ed仅一处锁与说明、一个完整行为用例，quote写入前屏障不替代真实SQL，finally保证事务释放。冻结后赛事审核/独立报价入库/结果台账/结算定向进行中，线上仍E222。E225完整Docker仍同66f91a6运行，两候选隔离，未在测试中修改源码。

15:19失败与重跑：首定向92项119.018秒exit1/1error，为test_independent_quote_ingestion导入test_fixture_snapshot_atomic时未包含tests目录；非业务断言通过，整个阶段保持未通过。新增并发用例单独1项3.277秒exit0，800ms引用通过/100ms写者阻挡/最终2.10报价正确。冻结源码未改，加入显式PYTHONPATH的原定向完整重跑中，不跳过故障模块、不扩大timeout。

15:28定向重跑100项129.205秒exit0；包含原故障模块，无跳过。冻结aee11ed完整Docker套件仍运行，未部署。E225独立66f91a6完整1257项548.600秒exit0，报告SHA73b23bb9d4c0e0389ca0d50d481ee58996d4db6bb51a6e21d9fcc984454305ea，仍不替代合并候选全套验收。
