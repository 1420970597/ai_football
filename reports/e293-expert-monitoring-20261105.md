# E293 自然专家消息完整监控

更新时间：2026-11-05 07:36 UTC。状态：真实联调完成，但稳定性及三秒延迟验收失败，继续修复。

目标：E290结果队列优化发布后，检查六群自然文图的持久化、Hermes实际回执、精确赛事/盘口及页面显示。责任目录：services/collector/、services/api/、integrations/hermes/、web/app/、reports/。前置：远端Telegram既有授权、旧网关deepseek-v4.1-flash、官方比赛/盘口事实；不发送测试消息、不下注。

## 窗口与采集

06:52:43.644818—07:22:49.751857 UTC，56样本1806.1秒，保留容器ai-football-e293-independent-watch，watch/finalizer实际exit0。原始JSONL SHA256：999d859fe47cfbe367300638b24b76719cd0e78d4e3db89604fe4f550d9de215。

20个新帖、178个旧帖修订，共199创建版本（含同帖后续版本，不能相加冒充199新帖）。新帖11ready、0pending、9unsupported；9项全部无正文，实际4 audio/ogg、3 video/webm、2 video/mp4；未把这些媒体当成功图片审核。11项eligible均有任务、无已确认缺失回执；截止6项在途，之后继续追踪到全部当前attempt双回执。

健康端点全200、六租约有效/单owner、采样异常0；但首样本TGstate unavailable，以及07:18:09单源ConnectTimeout/degraded均保留，整窗Telegram健康不通过。后续源自然恢复。远端07:17:23.323015650 POST events/no_response/ConnectTimeout耗时3295.6ms，E288仅claim路径恢复不能覆盖此故障；E295独立修复验证中。

API记录681个events请求全部200、数据库错误0；不能据此宣称整个网络正常。远端桥3152条慢/失败日志中，observed一次、fixtures两次503/BrokenPipeError，均downstream_reply；桥源码SHA06f022cd15ccfd218f3f995bf67a144a6da7cc81db94a17d830bde06b7128257未改变。日志为选择性慢/失败日志，不是全量请求分母。

## 真实审核

- 新帖2b0ebdc1-e39c-4cf9-8aec-07a85191ff25：六场亚冠二赛前推荐最终全部录入，町田泽维亚—上海申花小3；狮城水手—阿德莱德联主让0.5；杰志—金边皇冠小3.25；巴吞联—大埔主让1.75；柴桢—淡滨尼流浪者主让1；古晋城—江原FC小2.5。原文组合串关不另造独立选项。首次5项后有暂时盘口问题，第二attempt补齐到6，端到端905493.114ms、第二次模型审核419058.887ms。系统仍展示2项已恢复历史拒绝为“未解决”，这是回执语义缺口，不视为最终结论正确。
- 新帖130057dc-75ca-443a-92bd-8c91c695b6ca：四场赛前推荐全部录入，狮城水手主让1、町田主让1.5、江原客让0.75、巴吞联—大埔大3，当前系统保存价1.86/1.90/2.00/1.83。模型审核167509.246ms、端到端565072.188ms；实际工具存在排队/超时回退，不能称三秒。
- be3519a5九场欧冠原文已在消息发布前完场；实际唯一身份工具确认后未生成实时盘口/推荐。518ad53a图片旧场回顾，其中5场身份确认但完场、2场未唯一匹配，不能把这些未录入一概当新赛前匹配失败。后续仍需视觉检查原图证据。
- 其余7项为反馈、表情/梗图或没有投注意图，实际回执不录入；0a131c10原图已授权下载目视：谢珀顿U23—库巴厄XI U23下半49:54/0:4，半场0:4，未选中普通盘口，正文“收米”，不重开新推荐。

## 页面与命令证据

受保护证据在.runtime/e209-live/，不提交凭据或原始私有媒体。所有代码/DB/浏览器均Docker内运行，CPU和内存有界。

- bash e293-finalize-independent.sh：exit0；summary/current-versions/review-latency/API-phase/error-moments等完整窗口只读审计。
- storage-tunnel内执行e293-collector-timeouts.py、e293-bridge-final-audit.py：exit0，限定本项目和指定日志窗口。
- API内e293-media-final.py、e254-current-versions-audit.py：exit0；截止后的当前版本追踪记录在e293-current-versions-post04.json。
- Playwright e293-phase-clicks.mjs：exit0，1440/390共16场景，五比分标签实际API phase与列表ID一致、唯一aria-pressed、专家/会员/个人中心入口及无溢出；取消真实0行。
- Playwright e293-feedback-clicks.mjs：exit0，两端反馈详情、同attempt模型/系统回执、授权原图和0信号。
- Playwright e293-natural-text-clicks.mjs：exit0，两端四推荐逐卡当前比赛未开赛、同attempt回执和无溢出；最终六+四推荐再次双端检查进行中，未提前勾选。

## 下一步及失败处理

E292已自然idle后仅API切换，E294另起完整30分钟，不用短样本抵消本窗失败。E295仅对首次连接阶段失败做有界同体恢复，完整冻结后再发布。继续优化错误期间参数导致的零项重查、重试上下文和回执历史拒绝语义；不增加截止、不取消证据核验、不伪造快速结论。三个原滚球推荐的两小时官方结果跟踪独立继续，不能用专家反馈图替代赛果或提供方规则。

E296后续修复证据（07:53 UTC）：六+四推荐最终双端原点击用例曾在第二详情卡加载，两次诊断定位旧close在新弹窗open=true时到达并使新请求序号失效；原exit1/截图保留。E296独立修复并仅web发布后，确定竞态双端测试exit0，原自然6+4推荐4场景重跑exit0，详情与同attempt回执/当前阶段正确。一次登录429失败也保留，未放宽限流或清库。详见e296-dialog-reopen-race-20261105.md；不能将后续通过追溯为E293原窗口全部成功。
