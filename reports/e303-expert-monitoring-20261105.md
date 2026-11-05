# E303 连续自然消息监控

更新时间：2026-11-05 09:25 UTC。状态：真实联调中。

目标：继续核对六群自然文图的接收、当前版本审核、同attempt回执、比赛盘口与结算状态，以及端到端耗时。责任services/collector、services/api、integrations/hermes、infra、web/app、reports。前置E300实际API/worker同步、E301七历史误报已透明追加但精确numeric保留待修。独立保留容器ai-football-e303-independent-watch于09:16:18.628240 UTC启动，30分钟，健康、源状态、租约、当前eligible回执及收件延迟每30秒实际采样。自动收集脚本e303-finalize-wrapper.sh已运行，结束后生成全窗API错误及当前版本/延迟审计；远端桥及未支持媒体分别核对。不发测试TG或资金订单。

验收：所有可审核新消息有当前版本任务及同attempt模型/系统回执，逐一核对自然推荐原文与原图，无漏群、租约冲突及假健康；有故障保留并定位修复后再完整起窗。3秒明确仍未通过，不能把连续容器存活当完成。实际命令bash .runtime/e209-live/e303-independent-watch.sh启动exit0；finalize进行中。下一步完成本窗并研究模型及工具串行往返的延迟，不改正在执行的模型任务。
