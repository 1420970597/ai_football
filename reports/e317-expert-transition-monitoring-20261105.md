# E317 E315发布过渡观察

更新时间2026-11-05 12:18 UTC。状态：完整周期已核对，稳定验收未通过。目标覆盖API E311→E315切换，区分发布过渡和后续真实失败，责任infra/services/api/reports；使用同一现有六群、唯一worker与真实自然消息，未制造专家消息或交易。

11:42:42.138886至12:12:44.533006 UTC，56样本/1802.5秒，原始SHA2885a7dee68d994d2bd5e63e28eedb5a58e753d4268d15dadb0abc40a45c53ab。health/ready全200、租约无异常；11:43:52.944621、11:44:25.776648两次TG state unavailable，connected/authorized仍true，后恢复，不算全程健康。

桥接精确边界只读审计actual0：3007个200、47个503。45个发生在11:43发布切换时的上游断开/重置；另两个11:46:12.572393/11:46:43.942325为fixtures正常传回的503，对应API两个quote批量upsert执行超时，不归因于发布。未见此窗BrokenPipeError，不能据静默期判已修复E314断管。

两代API错误合并actual0：旧代归档e314-api-before-recreate.log退出0，仅筛本窗30行，合并新代精确本窗日志，保留两个TimeoutError：请求14362c3c/f1bfb026、backend3404203/3404209，prepare224.728/.013ms、execute4778.101/5001.917ms，均耗尽总5000ms预算，调用ingest_fixture_batch:135。当前只证明quote入库执行等待，不能仅凭旧E299 IO样本替这两个PID定因。下一步E318阶段计时与新的有界DB等待采样，不扩大预算或清理报价表。
