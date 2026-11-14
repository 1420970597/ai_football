# E232 正式监控窗口终审

更新时间：2026-11-04 17:59 UTC。结论：未通过持续稳定性与3秒延迟验收；继续监控，不终止会话。

生产固定bef4a43 API/worker，镜像736c2dff，配置/资源/其他服务在窗口内未修改。观察17:25:05.286219–17:55:08.121616 UTC，1802.8秒55样本；e232-watch-01.jsonl SHA256 5792b5d5723db86aa9d7d7421e10cdc04f52bd065c32d2a6f4a380bf497c4cb0，finished存在、stderr空；原启动未落单独退出码，因此不补造进程exit0。Docker exec导出及隔离window-summary解析均exit0。

healthz/readyz样本全200，6来源/6有效租约/1owner始终有效；第22/23样本17:36:56/17:37:29出现degraded=true/source TimeoutError各1，尽管全局connected/authorized为真，telegram_all_healthy=false。API完整同截点日志539次Telegram消息请求：live173×200+2×503、reconciliation336×200+3×503、delete23×200、unclassified2×200；跨接口19个Timeout，无截断。readyz日志也出现失败，不能用30秒离散健康样本全200声称连续无故障。审计管道保留pipefail/exit0。

末快照131新建档案版本，其中4自然新帖、127旧帖版本；5条当前自然合规消息（4新帖+1自然编辑），5任务5回执，0漏建/0缺回执。接收相对Telegram版本5条均在3秒内，P95约1.882秒，这是接收指标，不能冒称模型结论3秒。实时计数2175接收/87staged/0deferred/2088非配置来源；非配置来源不算丢失受信消息。

只读同时间边界e215-audit-window.py，exit0/4posts/1page/truncated=false；四条17:53新帖是“签到”“累了”和两张熊猫表情图，均首attempt完成、无推荐意图/0接受，符合原文；模型约8.960–12.506秒，event到结论约22.940–45.431秒，两条排队17.150/24.239秒。自然编辑67ce0abe revision3准确接受费内巴切对利物浦/全场主队+1.25/系统1.74，模型92.809秒/event107.024秒。旧帖编辑审计e232-live-versions-final.json为窗口结束之后对当前版本读取，未伪装历史快照，也未冒称与posted审计相同截止。

另两条正常管理员重试单独统计：真实图fe833486新任务dd01e81f一次成功接受费内巴切+1.25/系统1.74，原倍率2.01未作匹配条件；真实引用回复282eb80f新任务dbab7c84识别阿布扎比统一对哈尔迪亚，口语缺新盘口且盘口关闭，合规0接受。原失败证据与pending结算保留，0实盘交易。新结果桌面手机四项真实点击通过，详见E232主报告；不计入4自然新帖。

下一轮E234首启动17:55后因/tmp脚本import app路径缺失，ModuleNotFoundError/exit1/空样本，失败stderr保留在API /tmp/e234-watch-01.*；改为显式PYTHONPATH=/app后02轮已确认started17:57:14.768366，至少18:27:15以后终审，约2分7秒监控脚本间隙如实保留，生产服务与TG未停止。02轮保存独立退出码，正式代码仍bef4a43不变。E231/E234继续定位远端DB等待及模型/工具长尾，未完成不宣称专家链路全部正常。
