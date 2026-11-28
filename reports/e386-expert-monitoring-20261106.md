# E386 专家连续观察

更新时间2026-11-06 02:47 UTC。状态：真实联调未通过。目标覆盖E384生效后真实六群、租约、当前版本任务、同attempt双回执、API错误和自然延迟；责任运行监控、API与Hermes。原始采样容器ai-football-e386-independent-watch，Docker资源受限、只读数据库、30秒采样1800秒；未发送测试消息、订单或修改历史数据。

完整窗口02:07:41.994452至02:37:43.167310 UTC，1801.2秒/55样本，容器实际退出0；自动终审全部文件及cutoff已落盘，恢复后原PTY不可访问，不能凭此补造原父脚本退出码。独立读取的原始日志摘要06b7ce10d7dda338e565204ca9208901f3c34ca2497f3e7ed97f52ee6302cbda。存活/就绪全200、六源六有效单owner租约、deferred0，55样本连接/授权均真；首样本source_health.state=unavailable而connected/authorized真，该异常保留，telegram_all_healthy=false，不能称全窗健康。

本窗没有自然新发帖；四条旧帖自然编辑当前版本均ready、全部有任务，至截止仍retry/provider_transient，无终态双回执，不当作审核成功或零推荐正确。另191旧源版本补采单列。四版本接入平均10050.9ms/P9513019.7ms，0达三秒，未完成模型任务不计端到端成功。

624条TG持久化请求全部200（232 live、384 reconciliation、7 delete、1未分类）；live中位1125.7/P951558.7/max1795.6ms，补采中位2423.8/P953138.6/max6628.6ms。另02:29:32.474735报价upsert_quotes数据库执行5002.104ms超时，原5秒预算保留，属于赛事报价链，不能被TG全部200掩盖；后续需继续有界数据库等待取证，不假称已解。

下一步：E388跟踪提供方恢复，持久任务继续自然退避；E387受控单API生效后E389新完整窗，复核当前版本和真实图文，不将无新帖或短探针替代业务验收。命令docker wait/logs、Docker内window-summary/event-phase-audit与API只读current-versions审计，均按真实窗口边界；终审结果已可读，后置独立断言另留退出码。

02:49 UTC后置e386-final-behavior-check.py在Docker/network none实际退出0，逐项确认完整窗口摘要、0自然新帖/4当前编辑全部provider_transient重试、无漏建任务、624全部200、一报价超时和首状态异常均原样保留；business_completion_passed=false、three_second_target_passed=false。检查脚本退出0只表示证据一致，不把业务未通过改成通过。
