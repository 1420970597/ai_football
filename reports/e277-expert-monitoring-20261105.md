# E277：结算修复后完整专家监控

更新2026-11-05 04:31 UTC。状态：完整观察与只读终审通过执行，稳定性及三秒目标未通过；继续E280，不宣称最终完成。

目标核对E276 API/E274 worker下真实群组接收、审核、模型与结算，不改变生产。责任services/collector、services/api、integrations/hermes及监控证据；前置E276完整回归、42计划结算变化/251最新事实离线一致与双端浏览器通过。当前缺口是自然新推荐样本、编辑送达延迟及单群状态瞬时连接超时。

周期03:57:31.233227至04:27:33.872959 UTC，56次采样1802.6秒，watch和finalizer actual exit0。日志SHA256 1c6fa8719226f7f195163916f9bd906a3cd67bd914909b24776ae8fcc51be9dc，证据.runtime/e209-live/e277-*。API健康全200、错误审计0、六租约全有效，API/worker restart0。首样本TG state unavailable但connected/authorized；04:16:53及04:17:26单群ConnectTimeout降级，04:17:58恢复，不能称TG全程健康。同截止桥3057条慢/错误日志全部200，不代表全部请求数量；桥源码SHA06f022cd15ccfd218f3f995bf67a144a6da7cc81db94a17d830bde06b7128257未改。

191消息版本包含4新帖、187旧修订；新帖2ready、2unsupported，未持久化媒体pending0。两条ready原文均“吃饭”，模型说明与原文一致无推荐，系统同attempt回执完整。两条unsupported的实际manifest均video/mp4（1337478/71750字节），无正文，当前仅视觉图片能力不送作图片，未冒充审核成功。还有一条旧“⚡”编辑，两回执完整。合计3个模型结果/3个同attempt系统回执，没有漏建任务或在途实时审核。

原始eligible_current_reviews计数4/with_receipt3/missing_receipt1保留。第4项是10月24日旧消息在本周期新编辑，job historical_replay、attempts0、无attempt_id，last_error historical_backlog_auto_suppressed_by_operator及telegram_auto_review_suppressed原文/媒体保留回执齐全，按用户停止积压历史要求不送模型。不能把抑制trace说成模型审核。E280在原计数外增加已验证历史抑制回执数，便于分开识别。

两条新文本事件至结论18305.325/17759.163ms；provider7446.020/7494.818ms，模型12505.047/11474.354ms。三秒失败。旧⚡编辑从source_version至接收119306.4ms，另一条历史抑制编辑102015.5ms；新发言约2—3.6秒送达，不把历史编辑混成新帖平均延迟。限定04:06:30—04:09:00远端采集59条数字日志均200，多数事件提交1—3秒，不能凭无消息关联的日志确定119秒根因；继续核查。

E279候选提示改动完整回归1339项1error/exit1，持久化配置指纹校验失败，未发布。生产API/worker及其他服务均未因该候选改变。E280从04:28:08.890508 UTC接续完整30分钟；当前不生成虚假推荐或真实订单，不清理/改写原台账。
