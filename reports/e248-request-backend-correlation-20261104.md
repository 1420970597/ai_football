# E248 超时请求与数据库进程关联

2026-11-04 22:33 UTC，状态：隔离定向验证通过，未部署。

2026-11-04 22:44 UTC更新：冻结27c816923338fde2435d4285c8b9267538c4d534完整1311项614.110秒exit0（证据封装616.695秒），零失败错误跳过，committed_source_verified=true。独立1431927/backend-tests.json摘要912986a60f7f374bed1909969416ec5a5814aefcd7fd8d72529b30bacce0599e。候选镜像dfec8b7c5042693835c912b87e07187e839c72f943097408aa4251135c778d5c、完整revision一致，受限Docker build及Compose check exit0。原样cherry-pick为4d6af4f；API镜像相对当前生产仅main/新诊断及已有E246工作器模块不同，其他API模块摘要一致。框架/MCP未改，继承原验证，当前22:48:41前不部署。

2026-11-04 22:52 UTC更新：前一窗口完整终审后，22:49:36暂停/22:49:40自然idle，`bash .runtime/e209-live/apply-e248.sh deploy`实际exit0、门禁release exit0，仅API于22:50:16.995250737Z切换上述候选。只读校验exit0：API环境总摘要及预算不变，其他六容器ID/启动时间/预算完全相同；38推荐/报价、38结果、80台账及时间一致。未执行重建写入或资金交易。新独立1800秒窗口22:51:17.022933开始，到23:21:18后终审，日志持续留存；诊断上线本身不是性能或稳定性通过。

目标是将真实API错误的request_id与只读pg_stat_activity采样精确关联，责任services/api与tests。当前跨接口Timeout只有代码位置，不能从时间相邻断言数据库或传输根因。此项补齐诊断，不声称已解决性能问题。

实现从异常栈的self中仅接受默认asyncpg.Connection具体类型，调用驱动本地get_server_pid，不查询数据库，不序列化SQL、参数、异常正文或其他局部变量。最多检查64帧、收集8个去重正整数，只追加内部错误日志backend_pids。外部响应、原503/未确认写入语义、连接池及所有截止不变；非连接对象或元数据读取失败输出none，不能覆盖原错误。

Docker临时独立PG定向01为11项9.134秒exit0，补充元数据读取异常验证后02为12项10.784秒exit0，无失败错误跳过。命令`bash .runtime/e209-live/e244-target.sh /root/ai_football/.runtime/e248-candidate tests.test_api_error_logging tests.test_api_database_failures`，runner .5CPU/512MiB、PG .25CPU/256MiB。真实命令超时日志pid与连接pid相同且去重，SQL未重放，响应不含pid；查询取消及锁冲突仍503、随后连接可用；伪对象方法不执行、凭据文本不输出，读取pid失败不改变异常处理。完整冻结套件及当前观察窗结束前不部署；失败保留日志和原分类。后续必须用同pid等待证据定位根因，不能以诊断项上线称稳定通过。
