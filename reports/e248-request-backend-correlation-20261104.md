# E248 超时请求与数据库进程关联

2026-11-04 22:33 UTC，状态：隔离定向验证通过，未部署。

目标是将真实API错误的request_id与只读pg_stat_activity采样精确关联，责任services/api与tests。当前跨接口Timeout只有代码位置，不能从时间相邻断言数据库或传输根因。此项补齐诊断，不声称已解决性能问题。

实现从异常栈的self中仅接受默认asyncpg.Connection具体类型，调用驱动本地get_server_pid，不查询数据库，不序列化SQL、参数、异常正文或其他局部变量。最多检查64帧、收集8个去重正整数，只追加内部错误日志backend_pids。外部响应、原503/未确认写入语义、连接池及所有截止不变；非连接对象或元数据读取失败输出none，不能覆盖原错误。

Docker临时独立PG定向01为11项9.134秒exit0，补充元数据读取异常验证后02为12项10.784秒exit0，无失败错误跳过。命令`bash .runtime/e209-live/e244-target.sh /root/ai_football/.runtime/e248-candidate tests.test_api_error_logging tests.test_api_database_failures`，runner .5CPU/512MiB、PG .25CPU/256MiB。真实命令超时日志pid与连接pid相同且去重，SQL未重放，响应不含pid；查询取消及锁冲突仍503、随后连接可用；伪对象方法不执行、凭据文本不输出，读取pid失败不改变异常处理。完整冻结套件及当前观察窗结束前不部署；失败保留日志和原分类。后续必须用同pid等待证据定位根因，不能以诊断项上线称稳定通过。
