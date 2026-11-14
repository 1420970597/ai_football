# E215 自然消息监控

更新时间：2026-11-04 12:22 UTC。状态：当前自然消息回执齐全；整窗稳定性未通过，继续处理。

首窗 11:45:09.257771 UTC 的诊断 trace 聚合 TimeoutError，退出1。原始记录及stderr保留，未计作完整验收。第二窗诊断改用现有job索引按updated/运行任务读取轨迹，显式记录sample_error，不以失败填零；该轨迹计数范围不是任意静态历史任务的完整全库统计。

第二窗 11:48:39.284072–12:18:42.118060 UTC，1802.8秒、55样本，Python退出0，SHA256 `b8d2a25ba1c05bb15ab535e5668d194f570088db92ec4a07278bde1301c6bcbf`。6/6租约全窗单所有者有效；sample42就绪请求超时，sample14–17来源TimeoutError/recent_deferred，49–50来源HTTPStatusError，随后恢复，故health_all_200/telegram_all_healthy均false。回调增1794 received/79 staged/0 deferred，不能替代逐帖检查。

精确窗口提交后只读键集审计退出0：10条自然消息、未截断，6条当前eligible、均首次attempt且模型1/回执1，状态均unresolved；两条非当前消息已删除或编辑，旧任务失效原因分别source_deleted/source_edited。本窗6条审核原文及模型检查包含闲聊、表情、赛果反馈及一个15分钟区间大小推荐；最后一项已绑定费内巴切U19—利物浦U19，但玩法未支持而未录入，不把无信号一律当模型故障。该限制仍未解决。当前真实消息没有遗漏任务或已完成却缺回执，但样本小且不是完整Telegram服务端独立对账，不声称绝对零漏采。

另有04:55:35–11:45:27自然消息补审，100条键集分页3页共235帖、truncated=false，175当前eligible全有模型/回执，23生成推荐、152未解决、14多attempt。该补审时段不是本次30分钟窗口。近期真实AEK图文“大4.5/5”生成4.75系统价2.0推荐，但完场后因非零滚球比分规则unknown仍待结算，此问题未闭合。

Docker脱敏API日志退出0：live161次200/P952763.0ms、另1次503；reconciliation323次200/P955133.1ms；delete28次200/P955531.4ms。没有补偿503不表示无来源故障。单次SELECT1稳定样本约255ms，领取约3–5秒，真实模型简单信息约8–22秒、完整推荐可达97–122秒，端到端三秒未通过。独立SDK low/none算术探针约3.4秒、默认一次10.7秒，非随机充分对照，生产reasoning未改。

存储隧道0.1CPU/128MiB，cgroup增量830有效周期中203触发限流（24.46%），throttled_usec增19860093，usage_usec增2482663；这是限流计数，不等于墙钟延迟或全部超时根因。后续预算调整需独立对比，不在冻结窗口中修改。

证据：`.runtime/e209-live/e215-{watch-01.jsonl,watch-01.stderr,watch-02.jsonl,summary-final.json,audit-final.json,prior-period-audit-paged.json,window-traces-mid.json,suppressed-current.json,api-metrics-final.json,tunnel-cpu-delta.json,effort-probe.jsonl}`。当前运行API/worker未改；窗口结束后独立已验证E216上下文切换，继续新完整窗口及真实俄杯重审，不停止采集/审核，不执行投注。
