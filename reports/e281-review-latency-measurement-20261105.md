# E281：真实审核阶段耗时定位

更新2026-11-05 04:35 UTC。状态：研究中，生产代码不变。目标区分Hermes接口、配置/证据往返及CPU限额的延迟；责任services/api、integrations/hermes、infra；前置E277两条真实文本回执完整，三秒验收失败。不能把SDK微型请求或文本减少宣称为全链路提速。

只读查询两任务attempt_claimed/hermes_request/hermes_result/system_review_result时间，actual exit0，原始e281-review-phase-read.json。领取trace至hermes_request分别3980.098/4602.887ms；hermes_request至hermes_result9779.353/10478.752ms；其间Hermes HTTP7494.818/7446.020ms，剩余还含往返与完成证据核验。created_at是数据库事务时间，不是每阶段客户端单调计时，不能直接当精确配置纯耗时。provider_elapsed_ms在客户端代码中定义为整个Hermes HTTP请求，不是纯上游推理。

当前Hermes cgroup CPU max55000/100000，累计nr_throttled1433/throttled_usec79417686，仅累计总值不能证明当前请求瓶颈。启动受现有限额约束的只读CPU5秒采样与自然消息对照，不更改CPU、不发假消息、不执行模型人工推荐。缺少自然样本时保持样本不足。任何实现需先有可复核瓶颈、保持模型配置指纹、完整审核证据与权限；失败保留原记录、原检查重跑。

05:03自然样本结论：CPU监控265次/1320秒自然完成actual exit0。已覆盖真实新图片审核，19个5秒区间累计CPU3.350878秒/节流0.594509秒；对应事件结论84.454秒，不支持主要瓶颈为CPU限额，保持预算不变。新文本Hermes HTTP4.724秒/结论14.881秒；原provider_elapsed字段仍按Hermes整体请求解释，不当纯推理。真实图片工具五次约5.69/6.30（并行检索）/2.95解析/8.50读盘/10.74录入秒，各有持久化证据，继续保持校验；E284仅针对重复配置确认，收益须发布后实测。
