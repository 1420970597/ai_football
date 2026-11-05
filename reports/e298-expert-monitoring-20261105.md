# E298 连续自然消息与发布后审核验证

更新时间：2026-11-05 08:23 UTC。状态：真实监控中，未完成整窗验收。

目标：核对E295采集连接恢复、E297期间别名与拒绝回执修复生效后的连续六群采集、当前消息版本、同attempt模型及系统回执、自然文图精确匹配与结算。责任services/collector、services/api、integrations/hermes、web/app、tests、reports；前置为两个冻结完整套件通过及顺序单服务发布。生产数据不回滚，不写测试消息，不执行实盘投注。

独立保留容器ai-football-e298-independent-watch于08:11:24.349399 UTC开始，至少1800秒；另有E293原三推荐两小时只读跟踪。当前缺口包括整个窗口稳定性、新自然推荐对期间别名及恢复回执的实际覆盖、三秒延迟及不明确的上游滚球计分口径。没有实际命中相应分支时不得宣称该修复已完成真实验收。

08:20:34有界只读当前版本核对exit0：3条eligible全部同attempt双回执，0缺任务/在途/缺回执。其中两条闲聊及一条六合彩互动图片均没有足球推荐，模型判定无新增推荐，不能用此空推荐样本证明比赛命中。模型10.713/33.481/20.707秒，事件至结论31.664/38.369/34.319秒，三秒目标失败。六群连接/六租约单owner，无当前source_errors；最后结论以全窗终审为准。

补齐E294未验的自然图文：c0c75619-34de-42d7-912d-728552306672“町田泽维亚 -1.5”，原图显示町田泽维亚—上海申花/亚冠联2及开赛前盘口，不能从未选中的图片赔率替代正文意图。当前实际保存系统报价1.82，原模型及系统同attempt、accepted1。Docker真实浏览器1440/390两场景actual exit0，原图200/全媒体加载、原文/系统回执/当前未开赛状态/无横向溢出通过；只复用受保护会话，不反复登录。证据.runtime/e209-live/e298-remaining-image-browser.exit、e298-remaining-image-output/natural-clicks.json及原图original-0-0.jpg，原图已目视。旧E294四unsupported经manifest核实均无正文video/mp4，不冒充漏掉的图片推荐。

08:20:36官方结果跟踪exit0：5839209已confirmed，旧上半大3.75因总进球/剩余进球结果不同仍pending，新全场小8.5已win/+0.64；5821646新增confirmed完场4:1、半场1:1，但原上半小2.25两解分别半赢/全赢，仍pending/live_market_score_rule_unknown；5827395仍无官方确认。不能依专家反馈或猜测口径填收益。

实际命令为e298-independent-watch.sh启动容器、e254-current-versions-audit.py指定本窗起点、e294-four-result-read.py只读查询，以及固定浏览器镜像运行e298-remaining-image-clicks.mjs，均在Docker并限制CPU/内存。结束后保存docker wait真实退出、完整日志摘要、API/桥错误与时延，失败继续定位修复并另起完整30分钟；当前不结束会话，不称完整交付。
