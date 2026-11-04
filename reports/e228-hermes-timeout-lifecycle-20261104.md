# E228 Hermes 超时生命周期调查

更新时间：2026-11-04 15:28 UTC。状态：研究中，未修改生产。

目标：确认600秒审核超时/任务租约结束后，框架是否停止该attempt的模型工作，避免过期工作争用CPU和模型并发。责任目录services/api、tests、reports、docs/platform-v2；依赖固定Hermes v0.22.5源码和真实只读状态。现有worker会在finally删除expert-review专用session，但框架删除接口对active_write_guard返回409，删除本身不执行interrupt。日志出现3次session cleanup HTTPStatusError，尚未证明都是409或残留任务。

15:24左右只读health/detailed返回active_runs=0、stored_runs=0、valid running review jobs=0，metrics为零；此快照不能证明过去不存在残留，也不能把计数为零解释成历史调用成功。框架SSE断开路径存在interrupt/reap处理；普通非流式调用取消是否覆盖线程执行仍须逐层核对。不得根据上述风险直接重启、取消其他有效任务或修改框架全局源码。

设计与验收待核对后记录：候选如采用SSE，必须保留整体截止、图片结构、幂等与session、最终finish_reason、usage及不完整响应失败语义，并实测断开取消；不能用部分输出代替已接受工具回执。先做有边界生命周期复现，再决定实现。现阶段优先完成已冻结E227完整回归和合并部署，E228不混入候选。
