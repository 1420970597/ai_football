# E246 审核领取准备的数据库往返合并

状态：定向隔离验证通过，未部署。2026-11-04 21:54 UTC。

上一完整自然窗口13任务只读审计exit0，领取耗时min/median/max2950/3143/4453ms，事件至领取3589/28576/132961ms，provider3005/6552/42622ms，事件至最终结论20060/48747/147983ms。不能把所有延迟说成模型耗时，三秒未通过。

责任services/api/app、tests。保持原调度advisory事务锁前置不动；将耗尽任务回收、来源/专家轮次补齐、lane锁读三次SQL合为一次参数化数据库调用。依赖链CTE明确先回收再补来源、专家、锁lane；后续候选查询仍为独立语句，能看到本次新插入元数据，不误用写CTE共享快照。公平轮次、来源及作者授权、消息资格、分页、批量上限、SKIP LOCKED、并发和重试策略不变，无迁移、无新凭据、不增预算。

纯SQL准备抽入review_scheduler_setup，重试条件构造也移入并由worker兼容引用，避免两个策略副本。新增真实临时PG验证：单数据库调用可创建新映射但不重置既有轮次/并发；后续失败能回滚任务回收与全部元数据；批量上限一仍受限、下一领取看见新专家并按原原因处理过期与不可恢复重试。原锁竞争、耗尽锁跳过、持久重试、公平、lease/handoff及消息新鲜度测试一并运行。

定向命令bash .runtime/e209-live/e244-target.sh /root/ai_football/.runtime/e246-candidate tests.test_review_worker tests.test_review_freshness_scheduler，83项168.390秒exit0、零失败错误跳过，runner .5CPU512MiB/临时PG .25CPU256MiB，生产零测试数据写入。结构上减少两次准备往返不是实际延迟达标证据，尚须冻结完整套件、当前正式窗结束后门禁更新及下一完整窗口真实任务测量；失败保留证据并回退候选，不降低租约/绑定/价格或结算要求。

2026-11-04 22:11 UTC更新：冻结5836cec2e0314db2a36485456bd46621c70174be完整1308项613.692秒exit0、零失败错误跳过，独立证据1295726/backend-tests.json committed_source_verified=true，SHA3c7f4b857d5a69a45f3881b6fa96d1152b27cd1ba263ca793a94ca077e7d3b53。同源镜像2fe48241c624a1daeab530ab42730449bf81a2f314686f149d0f86b9428b8ca5构建、两次Compose检查均exit0。原样移入93bf97c并推送，生产仍旧E244工作器，当前完整窗口22:15:11后终审才切换。静态核对API模块没有导入worker，此变更仅工作器执行；部署范围缩小为review-worker，保留API及其他六服务ID/启动时间/预算。框架/MCP不变继承E235，不把单元或SDK探针当新框架/三秒验收。
