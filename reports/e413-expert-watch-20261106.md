# E413 专家链路部署前完整观察

更新时间2026-11-06 07:19 UTC。结论：采集与审核完整性通过本窗，端到端性能和报价写超时仍未解决。责任Telegram接入、Collector、Hermes、review worker、API以及报价数据库；依赖授权六群、远端生产库和当前deepseek-v4.1-flash路由。未生成测试帖、未改生产数据、未下单。

完整窗口2026-11-06T06:46:05.452164Z至07:16:08.215651Z，1802.8秒55样本。全部API健康/就绪及Telegram状态正常、六群租约全在且单owner，deferred始终为0。自然新帖子4条、ready 4；当前版本审计4/4各有一次模型回执及一次系统审核回执，无缺任务或回执。旧帖编辑和历史补采另计，不混为新发言。

只读原文/当前attempt记录确认：两条短回复、一个表情和一条赛果回顾+拉群推广（含一张历史晒单图），均无新推荐指示。四条正确保持无推荐、无信号，不因状态unresolved误称推荐审核失败。当前无该窗口真实推荐意图，因此本窗不能证明命中新盘口或推荐录入路径。

端到端结论14.826–54.497秒，中位27.907秒，0/4在3秒以内；provider耗时中位19.702秒，模型耗时中位23.241秒。四次输入准备全部低于3秒、worker领取调度中位1.500秒，主要延迟仍在上游推理及部分队列。本窗不支持“三秒已达标”。

API事件阶段共643请求，live226、reconciliation389、delete24、unclassified4，全部HTTP200，无请求失败；但两次报价upsert TimeoutError，完整堆栈落在quote_ingestion.upsert_quotes，生产报价写路径仍不完全健康。E405此前在超时同PID发现连续DataFileRead、无阻塞；本窗没有相同时间等待采样，不能将旧观察冒充两次新超时的根因。

报告机读证据：.runtime/e209-live/e413-summary-final.json、e413-current-versions-final.json、e413-review-latency-final.json、e413-audit-final.json、e413-api-phases-final.json、e413-reasons-final.json。`bash .runtime/e209-live/e413-finalize-independent.sh`退出0，窗口、当前attempt、trace、API phase审计均经Docker限额运行；compact summary断言退出0，`missing_double_receipts=[]`。浏览器另外以真实生产原图做1440/390宽度六次点击均退出0，三张原图逐张解码、当前attempt双回执一致、无溢出和JS异常；点击不写生产。

其后E411批量保存候选通过独立完整1534项测试和镜像核验，部署前自然空闲门07:18:33.595082 UTC持有。API更新结束后须另开部署后监控窗；当前观察不是变更后验收。E414/E416两种报价SQL草案在临时PG明显退化并停止，未改变线上upsert。后续依赖Hermes上游实测降时、quote写入超时同PID采样和真实推荐样本；在这些证据通过前保持“未完全解决”。
