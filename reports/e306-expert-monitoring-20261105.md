# E306 连续审核及赛事锁等待跟踪

更新时间：2026-11-05 09:50 UTC。状态：真实联调中。

目标：在E303一次fixture锁超时后继续六群自然图文、回执、赛事盘口、结算及延迟观察。责任services/api、services/collector、integrations/hermes、infra、web/app、reports。依赖E300生产API/worker及E304历史精度维护完成；E305录入原子写候选仍完整冻结，未上线。独立容器ai-football-e306-independent-watch于09:46:29.636043 UTC启动，每30秒实际采样至少1800秒，e306-finalize-independent.sh按真实started/cutoff自动完成API错误/当前版本/回执及延迟审计，不用硬编码边界。启动exit0，期末验收未执行。

辅助10分钟/1秒同项目只读活动采样区分固定表操作与advisory锁，纳入本项目维护application_name便于排除自身影响；不输出SQL文本、参数或凭据，不修改锁预算/数据。验收所有自然可审核消息当前同attempt双回执，逐图看原文与盘口选项；赛事写入/远端桥无错和模型延迟独立判断。失败保留原因，定位后修复，再起完整窗。若E305通过后本窗内发布，仅API按natural idle更换，记录真实时间且发布后需另完整周期，不能把跨版本窗口当新版完整稳定证明。不写测试消息或实盘订单。

## 完整窗口归档（2026-11-05 10:24 UTC）

09:46:29.636043—10:16:32.129552，56次/1802.6秒，watch/finalize/两代API日志合并检查均actual0，监控SHA256 `4e38bf4db95f883914c418292eee214a8203d1d3cfdcb1749cd3f1ea3329c105`。47条新发/35 ready/12 unsupported，158条旧文修订；36条当前应审（含窗口内旧文修订）全部模型+系统回执，无缺任务/缺回执。API发布前跟随日志和发布后日志合并均无ERROR，不能仅以后一个容器声称全周期无错。

本窗口不判连续稳定通过：09:53:40发布期间healthz/readyz采样不可达；09:54:14/46 Telegram状态查询unavailable但连接与授权仍true，六来源租约无异常。E307重新从新API启动后计时。DB只读10分钟601采样1331观察，仅09:53:05/06两次review_job_state锁等待，时间与本次自然空闲发布门重合；不能据此证明09:32旧fixture INSERT失败的阻塞者。最大quote DataFileRead 1.083秒，无锁阻塞。五条新增自然图片推荐原图和双端浏览器已核验，延迟仍超3秒。

实际证据：`.runtime/e209-live/e306-summary-final.json`、`e306-current-versions-final.json`、`e306-error-moments-combined.json`、`e306-db-wait-final-summary.json`。下一步E307完整窗口、E308冻结验证后减少模型检索往返，继续查未知计分规则及新增BTTS缺盘口问题；不把停盘、旧单反馈、无推荐口号合成为可投注信号。
