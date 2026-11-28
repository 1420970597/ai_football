# E389 专家新发言完整观察

更新时间2026-11-06 03:18 UTC。状态：真实联调未通过。目标核对E387单API生效后的六群接收、完整新帖/修订、当前任务和同attempt双回执；责任运行监控、API、collector与Hermes。前置镜像及完整1507项、双端18浏览器场景已通过；自然发言与真实模型结论仍需独立验收，不以探针或样本缺失替代。

Docker受限容器ai-football-e389-independent-watch实际02:40:30.940846至03:10:31.560929 UTC，1800.6秒/55样本、退出0；e389-finalize-independent.sh退出0。原采样SHA256 ddb80ae91a80c3c6db015c1525b506067f8f21871bbdbb97de0eb8d5cc652c14。55样本存活/就绪200、TG connected/authorized真、六源六租约且同一owner、deferred增量0；首样本state=unavailable与连接授权不一致，保留异常不称全绿。

五条自然新帖：四条ready均有当前任务且截止仍retry/provider_transient，无hermes_result或system_review_result；一条unsupported且无审核任务。对这条媒体单独只读核验实际0：零正文、video/mp4、128810字节、222×228、allow_media=true，属于目前只支持图片的真实范围，不能误报漏收图片或成功审核。四条ready发帖至接入1743.0/2748.9/2162.3/1468.9ms，MP4为10641.2ms；五条平均3752.9/P959062.8ms，4/5三秒内，仅接入而非模型结论。四项初始message_to_job有两项5023.8/3593.3ms，应继续追踪媒体/队列时序；退避后的queue_wait不能冒充首次排队。

另179个旧帖版本补采单列，不冒充新专家推荐。628条TG持久化请求全200（229 live/386 reconciliation/9 delete/4未分类），无API数据库错误。live P50 1114.2/P95 1528.9/max2876.7ms，补采P50 2397.8/P95 2728.0/max6939.6ms。本窗无报价5秒错误不证明E386偶发根因解决。

验收失败原因：四条有效图文无模型与系统最终回执，三秒端到端目标未达。既有任务继续原持久退避；不清历史、不把提供方故障改为零推荐成功。下一E391完整1800秒窗已于03:13:25.998912 UTC开始，六源六租约正常，自动终审已启动；模型独立复测03:13:18.982700Z返回503/413.610ms，详见E388。失败处理继续分层取证、原帖当前版本逐项核对；恢复必须以真实图文工具回执及新整窗证据确认。

实际命令：Docker compose run监控、docker wait/logs、限额Docker内window-summary/event-phase-audit、API内只读e215-audit-window/e389-current-versions-audit/e254-review-latency及e389-media-inspection。终审退出0只代表证据生成成功，不是业务验收通过。
