# E354 专家真实消息完整观察窗

更新时间2026-11-05 20:40 UTC。状态：真实联调部分通过，延迟与稳定性未通过。

目标：E351恢复后完整1800秒核对六群同时采集、当前消息任务/模型/系统回执、真实推荐入账与原图展示。责任docs/reports/tests，依赖已生效schema029/API与worker同源r2、既有TG授权及远端存储。只读观察，不发测试群消息、不写生产测试数据、不投注。

窗口2026-11-05 20:01:33.840786—20:31:36.292338 UTC，1802.6秒、56样本；watch及finalizer实际退出0，原始窗口SHA256 2939dbe1f349ea2ebfd17b93acfe57ed684ebfa61d5c7c0863d48760b78aeff7。命令bash .runtime/e209-live/e354-independent-watch.sh / e354-finalize-independent.sh；数据库审计明确只读、有界键集分页且truncated=false。

已核验证据：

- 健康/就绪全200，全部样本六source租约有效且单owner；实时事件计数增量received1123/staged49/deferred0。unmapped1074来自未配置监控源，不能计为受信专家漏采。
- 23条自然新帖：19条ready，4条unsupported；19条当前可审新帖均有任务、当前attempt模型回执及系统回执19/19。窗口截止采样仍1条运行中，截止后终审已完成；没有把运行中当完成。另旧原图修订1条，单列而非新帖。
- 两条真实消息系统分别接受4条及1条，共5条推荐。四条为格拉茨风暴/桑德兰/布莱顿让球及本菲卡小球；一条为特温特上半场大0.75。只计系统接受结果，专家倍率不参与匹配，未替换原文盘口线。多场消息另5个意图均已识别赛事，但原文盘口线当前closed，保持未接受，没有改线补成成功。
- 1440/390真实Chromium六个详情场景actual0：两条成功消息显示4/1卡片、当前attempt与系统/模型回执一致；另一条原图实际decode成功两端均1张，无JS错误或横向溢出，production_writes=false/response_injection=false。命令Docker ai-football-browser运行e354-natural-browser.mjs，结果e354-natural-browser-output/live-clicks.json。
- 673条TG API审计全部200，其中live270/reconciliation390/delete5/其他8；live中位1106ms/P95 1483.7ms，reconciliation中位2400ms/P95 2673.9ms。

未通过与局限：20:13:44及20:14:17两次采样单源TimeoutError/lease_deferred，随后自然恢复；租约未丢，但telegram_all_healthy=false，不能称全窗健康。另赛事报价upsert_quotes有3个真实数据库5秒TimeoutError，分别20:09:59、20:15:18、20:24:21，属于赛事采集，不能被TG全部200掩盖。

19条新帖上游耗时最小4875.774ms/中位6670.277ms/最大146686.896ms；事件至系统结论最小14921.025ms/中位33716.618ms/最大520912.345ms，0条达到3秒。单条成功半场推荐131278.498ms，多场部分成功520912.345ms。模型内部工具轮次与队列/远端数据库往返均有成本，不能把并发实现或SQL次数减少当作三秒达标。

下一步：E358等价解析读取优化的冻结全套/受控生效及自然复核；E359接续完整1800秒；继续调查单源网络超时及报价锁等待，原结算unknown仍需官方明确口径。失败保留，不降低分页、绑定、报价新鲜度或收益证据要求。
