# E396 Hermes 推理参数实测

更新时间2026-11-06 04:31 UTC。固定实际Hermes的custom provider默认medium，API接受model_options.reasoning_effort。经现有Hermes入口对虚构签到消息做四次限额调用，不建立生产任务、不含真实账户、不直连模型提供方；每次独立会话清理。

medium两次18.461577/5.359993秒，none两次9.897199/6.177768秒，均HTTP200/stop且明确无推荐意图。usage分别为11173/25075/11192/10991总tokens，第二次none有额外上下文消耗，尚不能归因缓存或技能调用。样本少且none没有稳定优于medium，不修改生产参数、不把最短单次当3秒达标。首01缺source_name在发HTTP前KeyError失败1保留；补齐固定输入后02实际0。完整最小证据e396-framework-effort-probe-02.jsonl。

下一步减少实际数据库/工具往返和上下文冲突，先对真实工具轨迹核验。当前治理已持久化旧提示摘要，任何prompt改变须有可审查版本切换及消费证据，不能直接绕过摘要校验。
