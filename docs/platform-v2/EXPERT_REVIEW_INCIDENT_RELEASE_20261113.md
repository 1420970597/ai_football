# 11月13日专家漏识别事故修复续接

更新时间：2026-11-13 01:12 UTC（北京时间09:12）。业务修复已发布并读回；v3常驻巡检已启动并进入新闭合窗口。此记录替代旧文中“第54轮仍运行”的状态，不将候选发布称为整个平台完整交付。

## 窗口与根因

用户指定北京时间2026-11-13 01:00–03:00，对应UTC11月12日17:00–19:00。实际核验61个消息版本、60条独立发言，56个实时审核任务为55未解决/1成功；47无新增明确推荐、4检测截止、4已认出双方但来源期间未知、1接受，另5条unsupported缺内容不能判定意图。必要原图与回复已目视及SHA核对；不能将55未解决都当模型漏检。

确认多场清单原来只处理前两场便耗尽20秒，至少芬兰+0.5在原生历史证据中可录入却未录入；其它未读取项目不能追认为当时可成交。来源已有联赛被省略也造成真实/电子同名歧义。同专家队列新消息插队、两个空闲全局槽位未被使用加重等待。原巡检更卡住旧UTC13:00–13:30窗口，53次终态失败后仍第54次重复，后窗未覆盖。

## 本轮实际发布

业务源码为`91512d69e445dd55083390c39812dac6f597afa3`，权威树`.runtime/refactor-wt/web-ux`、分支`refactor/TASK-78-web-ux`。仅更新API与本地MCP API的来源方向映射/证据、worker截止优先队列、有限批量MCP及Hermes技能/白名单；保留原模型提供方、账户、来源、资金许可和20秒门限。

多场工具一次最多6个不同对阵、内部2路，逐项完整分页、唯一身份绑定、精确期间/方向/所选方线及当前报价核验；来源可读联赛必须声明，未知显式null。让球不用重复market_query。精确接受后的回执去掉重复候选与行情，原生record_result保持，完整原生证据仍留API；未知/歧义/停盘/缺期间仍不录入。不能按库存补期间、默认滚球、改用邻线或沿用专家旧倍率。

00:58:25.277739–01:01:38.171012 UTC暂停新审核领取，在途租约为0；顺序重建五角色并核验API/Hermes/MCP/schema及真实数据库门禁，退出0。期间无新detected消息、无维护超时，检测时间未重置。五角色实际源文件SHA、镜像、完整环境值与原挂载读回符合候选；其余六容器身份、镜像及启动时间未变。无结构迁移（现38项）、无测试实盘投注、无历史重审补投。

当前镜像与两只读Hermes挂载以根检出及权威树同步的`infra/production/expert-review.json`为准。它必须在旧历史覆盖之后生效；不得从根检出的旧业务代码重新构建覆盖。运行服务有意采用实际旧基础镜像加本轮已冻结文件的薄层，保留先前首页及独立手动买入修复。collector、web、executor、原数据及恢复来源不变。

## 验证证据与界限

Docker冻结组合452项曾全部通过；最终受影响协议/预算/权限/技能141项全部通过，冻结文件SHA与上述业务提交一致。测试运行的父Git引用为8d68e53e，不能把未提交快照元数据写成已提交91512d69；发布报告记录这一区分。八项无socket发布故障模拟通过，涵盖反序回滚、真实持锁/心跳、全局截止及回滚失败停止review-worker。前几轮失败、原退出码及harness修正均保留。

真实模型第五轮task `af-incident-six-20261113005556-4167515`，原28行来源文字未改，专属临时库合成当前盘口：正例6/6、负例严格4/6，歧义/停盘各拦1；最后成功录入HTTP完整响应距首次detected为16.380131秒/8.296947秒，HTTP最大并发2；旧ASGI生命周期计数也另存。两例清理GET404、订单零。整轮HTTP为20.028904秒/17.141379秒；正例整轮不是20秒内。此证据只证明两条隔离链路正确推荐已在20秒内形成，不证明生产排队、多专家突发、图像准备、上游刷新或页面显示的全局容量。

上线后至01:03:49 UTC自然消息为0，样本不足，不报生产准确率或20秒达标率。具体镜像、SHA、配置核对与门禁见`reports/expert-review-incident-20261113-release.json`。详细人工核验、失败与源码证据在权威分支`docs/platform-v2/EXPERT_REVIEW_INCIDENT_20261113.md`及对应accuracy/scheduler/batch-budget/monitor报告；原始私有内容按原posted+72小时保留，不入Git。

## 巡检与剩余工作

v3镜像`sha256:018f93654bcf669de1743291664da38891f575e168194b0baa22a1e116592b05`把扫描、完整覆盖和修复backlog分开，最新闭合窗优先，单窗partial/失败不阻塞新窗；单fingerprint有界重试，无增量不重复规划提交。明确缺陷的真实修复按原冻结/验证/发布门禁执行，不因文档提交就标完成。

真实one-shot已在00:27:33–00:36:53 UTC完成23:30–00:00窗，核验24消息/22未解决，child exit0、有效partial、外层exit1符合契约，未改Git。旧失败和unsupported缺内容继续显示覆盖不健康；不能把存活包装成覆盖完整。四个人工17–19窗已真实导入partial/partial/complete/partial，历史53次失败与第54次中断保留。

新巡检登记来源阶段消歧能力缺口：完成全阶段搜索后，同队同联赛的停报赛前重影与来源支持的滚球仍无法按可靠来源阶段收窄。已有合成无DB最小复现，进入独立修复backlog，尚未证明该历史项有有效盘口，也尚未修复。不得简单删除不可交易候选或默认live消歧。来源期间缺失、unsupported可恢复性和多专家容量继续自然样本核验。

私有控制器环境及状态继续在`.runtime/expert-review-iteration-20261112/continuous/`；当前环境入口为`.runtime/expert-review-incident-20261113/monitor.env`。使用权威树`infra/monitor/compose.yml`与`compose.git.yml`，完成所有父代理源提交后启动，避免只读scan被并发Git提交误判。覆盖有真实缺口时Docker health会不健康；分别核对process_healthy、active_task与窗口推进，不禁用检查。

01:10:31.942223 UTC，权威树记录提交`3b01c2da8f0616544906a09e3407756be528c833`已推送并与远端引用核对后，以精确018f9365镜像启动常驻容器，Compose退出0。实际限制0.5 CPU/768MiB，process_healthy=true；新任务为UTC00:30–01:00（北京时间08:30–09:00），已产生真实子代理事件，600秒截止，前一23:30–00:00结果仍保留。coverage_healthy=false、17窗尚未扫描、1失败/4partial，水位缺口仍如实显示，不能将启动当作全量补齐。之后不并发改变权威源树Git引用，根检出单独提交本运行证据。常驻每个闭合半小时优先分析新窗；已确认缺陷按证据和预算进入独立修复，完成修复须再次通过冻结与发布门禁。

实际启动命令为`docker compose --project-directory /root/ai_football/.runtime/refactor-wt/web-ux --env-file /root/ai_football/.runtime/expert-review-incident-20261113/monitor.env -f /root/ai_football/.runtime/refactor-wt/web-ux/infra/monitor/compose.yml -f /root/ai_football/.runtime/refactor-wt/web-ux/infra/monitor/compose.git.yml up -d --no-deps --no-build --pull never supervisor`，退出0；Docker inspect和资源受限Docker内读取status/schedule/events均退出0。最新常驻启动证据保存在根检出发布报告；权威源分支同名报告为启动前历史快照，不能把pending_start当现状。
