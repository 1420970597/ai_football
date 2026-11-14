# E239 自然专家消息完整窗口

时间：2026-11-04 20:20:12.921497–20:50:15.717353 UTC。结论：消息对账通过此样本；稳定性及三秒端到端目标未通过。

生产 API/worker 为 E238，bridge 为 134a9f4 分段日志候选。监控实际 1802.9 秒、55 样本、exit0；终审同截点导出脚本 exit0。六来源租约始终有效、单 owner，健康采样全 200，但六次 Telegram 状态异常，且日志有采样间隙的 readyz 超时，不能称全程健康。

16 条窗口内自然帖子：12 ready 均有当前任务、模型结果及同 attempt 系统回执（3 succeeded/9 unresolved），没有缺任务或缺回执；其余四条逐条检查原始 media_manifest，均为 video/webm 动态贴纸，非被漏掉的静态推荐图。原文/版本分页审计没有截断，旧帖补传不计入自然新帖。LOL 电竞及无推荐指令短句没有录入足球推荐。

三条消息产生五条已接受推荐，独立只读核验原图及报价证据：多特蒙德 -1、阿森纳 -1.5、科莫 -0.5；PSV 对布鲁日上半场大 1.75；多特对贝蒂斯上半场大 0.5。客队选择线与原生主队线区分，系统赔率不与原文倍率作门禁比较。图片的总进球 2–3 区间缺少可录入对应盘口，保留未解决，没有改成大小球。原图经正常管理员浏览器解码核对；两次 blob 提取失败及后续 canvas 导出成功均保留，不篡改原文或页面内容。

图片任务首轮193.292秒、接受2项，因阿森纳盘口 refresh_unavailable 安排一次20秒市场恢复；第二轮284.511秒补齐3项，总事件到结论517.133秒。另两条文字模型耗时97.736/80.517秒，事件到结论107.354/137.759秒。最终图片模型原文包含英文和 DSML 标记；中文确定性业务回执与模型原文分别保留，前者正确不能替代模型输出质量验收。所涉比赛当时仍滚球、官方赛果未确认，未宣称实际账户盈利或已结算。

API 同窗口完整日志：8个 TimeoutError、1个 LockNotAvailableError。事件接收 live187/188成功，reconciliation335/339成功；上行与数据库均存在失败。桥首次 tail3000 输出有早段截断风险，标记 partial；按三个不超过10分钟的同截点分片重新导出2746条慢/失败记录、31个5xx，exit0，不把慢日志数量当全部请求数。

主要证据位于 `.runtime/e209-live/`：e239-watch-01.jsonl、e239-summary-final.json、e239-audit-final.json、e239-current-versions-final.json、e239-api-phases-final.json、e239-error-moments-final.json、e239-bridge-final.json、e239-natural-signals-audit.json、e242-retry-audit-02.json、e242-unsupported-audit-02.json。命令为受限 Docker 中 window-summary/event-phase-audit/e215-audit-window/e232-live-versions-audit 与只读桥日志分片导出；秘密不输出，未投注、未清理消息或台账。

后续 E241 精确亚洲让球别名修复于20:51:50/20:52:04更新 API/worker，完整1286项通过，15场重建及38推荐/80台账严格不变，双端真实浏览器4场景通过。新窗口实际从20:53:45.408739开始；首次只读rootfs复制和第二次模块路径启动失败均不计入新30分钟。网关21时前生成探测再次返回502/Upstream access forbidden，目录存在模型不等于生成健康；独立传输候选与后续真实恢复继续验证，不能标记全部解决。
