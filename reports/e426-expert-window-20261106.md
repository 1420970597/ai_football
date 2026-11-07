# E426 完整监控与E427等待采样

更新时间：2026-11-06 09:09 UTC。真实窗口08:26:27.678262–08:56:30.401357 UTC，1802.8秒、56样本。采集进程正常完成，不能据此称业务验收通过。

六群、六有效租约、单owner、deferred0。31自然新帖中23ready、8unsupported，186旧修订另列。截止采样23应审均有任务，9有回执、14在途。终审稍晚读取最新attempt时11双回执、12在途；这两组时间不同，不倒填截止结果。完整双回执断言actual1保留。

部署期间08:39:45一次健康请求未响应，08:40:18/51两次源state unavailable（连接/授权仍true），故全窗健康失败。授权远端collector有界只读确认08:39:41–08:40:00后端503/HTTPStatusError与API重建相邻，不能将其解释为TG已退出授权；另08:29:53存在TimeoutError。持久检查点与重试保留，后续窗口仍须核验漏审。

部署前后日志经同一时间边界拼接去重，688事件请求均200，但1报价upsert TimeoutError存在。API自身日志无法覆盖反向代理在API重建期间返回的503，远端采集器证据补充此限制。首拼接因非root无法读取0600证据失败，02使用root只读、不改文件权限，actual0；初次空输入统计0条不算通过。

11有结论样本事件到结论88.239–476.709秒、中位216.654秒，0达3秒；模型中位45.780秒、提供方41.525秒、排队189.436秒。回执完整不替代语义核验，模型恢复不等于延迟达标。

E427每秒只读1801样本/1800.231秒actual0。活动539样本、最长4.385秒；24阻塞样本最长1.832秒，主要transactionid、一次advisory；长IO16 DataFileRead/1 DataFileWrite。quote_upsert样本0，不能声称报价超时已根治。操作名other_project_query仅为本项目数据库内未细分SQL标签，不表示读取其他项目。

证据：.runtime/e209-live/e426-summary-final.json、e426-current-versions-final.json、e426-review-latency-final.json、e426-compact-final.json（退出1）、e426-api-phases-complete-02.json、e427-wait-summary.json、e430-remote-source-errors.json。实际命令为对应独立watch/finalize、容器内只读审计与combine-logs 02；不写测试消息、配置、订单或清理历史。E429及E431下一完整窗口继续，E430隐藏流式重连优化处于冻结完整回归，尚未部署。
