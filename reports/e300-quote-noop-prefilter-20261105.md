# E300 重复盘口写入预筛

更新时间：2026-11-05 08:32 UTC。状态：代码已写、定向隔离通过，完整冻结测试进行中，未发布。

目标：缓解E299同PID实证的当前盘口批量入库DataFileRead超时，减少相同/更旧报价的无效INSERT冲突探测。责任services/api、tests、infra、reports；前置E299研究设计、原唯一引用索引与五秒执行预算。不能把热点缓解保证为全部磁盘故障解决。

设计：在原单条INSERT前，按fixture/market/period/coalesce(line,0)/selection的同一唯一引用LEFT JOIN，只让不存在或满足原单调版本及内容差异条件的报价进入INSERT。提取固定SQL别名的共用条件，预筛与ON CONFLICT最终复核使用相同逻辑；保留原fixture锁、并发条件复核、稳定quote_id、完整快照关闭、未知上游时间按received_at顺序及结果保护。无迁移、无清理、无放宽新鲜度或超时。

候选冻结34247a8。e300-targeted.sh Docker临时具名PG，29项24.751秒actual exit0，覆盖独立报价时钟、null/zero引用冲突、2000完整快照、回滚、结果锁、缺失项关闭；新增相同/更旧报价在另一事务锁住quote时仍不等待写锁，并发更晚报价提交后仍由冲突条件阻止旧值覆盖。完整e300-full-suite.sh进行中，未结束前不得改冻结源码或切换生产。

隔离执行计划对照：同一2000报价批次，四轮交替基线/候选、每次事务回滚，EXPLAIN ANALYZE BUFFERS仅在临时测试DB。相同报价执行中位49.903→12.739ms、shared hit8000→37、冲突2000→0；1%变化49.120→14.591ms、冲突2000→20；全变化90.036→96.653ms，有约7.3%回退；更旧55.007→14.247ms、冲突2000→0。这是小型热缓存样本，不代表生产端到端延迟。小表允许seq scan，因此另用生产只读EXPLAIN SELECT（无ANALYZE/无写入）核对293万报价规模：Nested Loop+v2_quotes_reference_unique Index Scan，无全表扫描，exit0。

原始证据.runtime/e209-live/e300-targeted-01.exit/log、e300-quote-plan-probe-02.json/exit、e300-production-plan-read.json/exit。首次探针JSON类型解码错误exit1，修正探针后重跑0，业务未为此改变；一次向只读容器docker cp失败1，改用单次容器只读挂载，不修改生产容器。完整验证通过后，仍需原台账保护、准确镜像源码核对、等待E298整窗结束和自然idle单API发布，随后另启完整30分钟真实复验。收益不足、语义失败或新错误时保留记录并恢复前版镜像，不回滚原数据。
