# E360 有界数据库等待观察

更新时间2026-11-05 21:04 UTC。状态：研究中，已完成本次只读观察，尚未据此发布额外业务改动。目标为区分报价写入超时与审核延迟是否来自数据库锁、I/O或客户端往返；责任services/api及reports，依赖当前远端数据库业务只读权限及E354已保存超时记录。

设计与实施：使用项目Compose最小API配置启动独立0.1CPU/128MiB容器，连接设置default_transaction_read_only=on、单语句2秒；每秒采样一次pg_stat_activity，仅当前数据库/业务角色/项目application_name，最多40个活动会话。只输出操作类别、等待事件、耗时及阻塞PID，不输出SQL、参数或秘密。不连接其他项目，不取消生产查询。

实际命令：bash .runtime/e209-live/e360-db-wait-watch.sh，启动actual0；docker wait ai-football-e360-db-wait-watch确认actual0。容器日志601个样本/600.184秒，完整finished记录。docker logs保存至.runtime/e209-live/e360-db-wait-watch.jsonl；汇总命令docker run --rm -i --user 0:0 --network none --cpus .1 --memory 64m --entrypoint python -v /root/ai_football/.runtime/e209-live:/evidence:ro ai-football-api:2.0.0-dev-e358-6a49938 /evidence/e360-summarize.py，actual0，产物e360-summary-02.json。首轮未传-i导致0样本，为无效汇总并保留，不充作无等待证据。

观察：20:49:10.153与20:49:11.242UTC同一review_job_state出现transactionid等待，查询耗时0.715和1.714秒，阻塞PID相同；601样本中仅一次活动查询超过1秒。quote_update有3个DataFileRead样本，另7个项目WALSync样本。多数ClientRead表示服务端等待客户端后续指令，不能当作数据库执行慢；采样中没有报价行锁等待，不能由此排除采样间隙的等待，也不能把单条IO事件证明为全部5秒超时根因。

缺口与下一步：逐阶段远端往返和模型耗时已明显高于3秒；E359前23自然新帖provider最短4741.503/中位8598.389ms，端到端最短14899.161/中位22275.579ms，0条满足3秒。需把具体锁持有方与任务生命周期关联后才改锁顺序；报价需同一失败时段的查询计划/等待证据，不能盲删索引或扩大超时。任何改动仍要行为与并发恢复测试、冻结全套、受控部署和新真实完整窗口。

失败处理：采样权限、超时或数量边界失败必须保留退出码，不能改成空样本；不取消业务、不清理原数据、不用健康200代替真实审核延迟。
