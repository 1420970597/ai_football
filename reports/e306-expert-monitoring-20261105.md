# E306 连续审核及赛事锁等待跟踪

更新时间：2026-11-05 09:50 UTC。状态：真实联调中。

目标：在E303一次fixture锁超时后继续六群自然图文、回执、赛事盘口、结算及延迟观察。责任services/api、services/collector、integrations/hermes、infra、web/app、reports。依赖E300生产API/worker及E304历史精度维护完成；E305录入原子写候选仍完整冻结，未上线。独立容器ai-football-e306-independent-watch于09:46:29.636043 UTC启动，每30秒实际采样至少1800秒，e306-finalize-independent.sh按真实started/cutoff自动完成API错误/当前版本/回执及延迟审计，不用硬编码边界。启动exit0，期末验收未执行。

辅助10分钟/1秒同项目只读活动采样区分固定表操作与advisory锁，纳入本项目维护application_name便于排除自身影响；不输出SQL文本、参数或凭据，不修改锁预算/数据。验收所有自然可审核消息当前同attempt双回执，逐图看原文与盘口选项；赛事写入/远端桥无错和模型延迟独立判断。失败保留原因，定位后修复，再起完整窗。若E305通过后本窗内发布，仅API按natural idle更换，记录真实时间且发布后需另完整周期，不能把跨版本窗口当新版完整稳定证明。不写测试消息或实盘订单。
