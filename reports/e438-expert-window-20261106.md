# E438 部署后完整专家观察

2026-11-06 09:51:21.792336–10:21:24.237167 UTC，1802.4秒/56样本，观察与自动终审退出0、当前版本双回执核验0。

六群六租约单owner、连接/授权/来源/存活/就绪全窗正常，deferred增量0。7个自然新帖5ready/2unsupported，旧帖修订单列172；窗口内6个当前应审版本均有同attempt模型及系统回执，3 succeeded、3 unresolved。657次TG事件请求全200，该窗没有数据库错误。

不能据此判专家完全正确：88326a85期间误录仍存在，E443尚未部署；另外两条接受为43780868主队+0.25系统1.82和69dbdc53明确“全场3.5/4大”系统1.88。后一条文字与原图赛事实体相符，不能用它事后替代前一条歧义原文。其余为聊天/表情无推荐意图。

有完整时延字段的5条端到端中位21.802秒、范围20.635–105.674秒，0条达到3秒；6个双回执与5条时延样本分别记录，不伪造缺少的测量。provider中位9.684秒、领取1.582秒。实时事件HTTP中位1.113秒、P95 1.492秒，不等同于模型端到端。

命令：bash .runtime/e209-live/e438-finalize-independent.sh；Docker内e413-compact-summary-02.py e438；按6个message id有界只读e393-reasons-read-02.py，均实际0。证据.runtime/e209-live/e438-{summary-final,compact-summary,current-versions-final,review-latency-final,reasons-final,api-phases-final}.json。下一轮E444在10:21 UTC已启动，持续逐帖审核；E443冻测及受控发布后还需重新开始完整观察。
