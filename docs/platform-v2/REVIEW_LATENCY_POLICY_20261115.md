# 专家审核时延要求变更

2026-11-15：用户最新明确要求“20秒不作为系统硬要求”。本记录优先于历史交接中20秒上限。

目标：准确率与逐项完整性优先，不因首次检测后超过20秒取消审核、拒绝工具或禁止合理恢复。继续记录排队、首工具、首条和全部推荐时延，20秒仅作观测阈值。

责任：services/api审核政策与worker、工具API；infra/monitor持续验收。依赖：现有租约、报价时效、赛事状态和工具证据。

设计：默认无检测至推荐硬截止；保留现有单次网络/数据库超时、600秒任务资源保护、租约撤销、来源编辑删除、精确绑定、停盘和报价新鲜度校验。取消20秒不授权过期价格、失效比赛、历史补投或真实投注测试。

验收：超过20秒仍可继续检索/核验当前有效盘口；并发专家相互隔离；失效租约、旧版本、关闭/过期盘口仍拒绝；持续窗口不再仅因耗时超过20秒判失败，无发言和不确定语义仍不通过。保留旧周期历史，新政策独立状态记账。

状态：研究完成，默认检测截止与claim元数据已修改，正在更新监控验收和隔离行为测试。失败处理：保留失败输出、修复后重跑；不扩大资金权限、不重放旧推荐。下一步：冻结Docker回归，按闲置租约门禁发布必要服务，核验自然消息与持续观测。

15:52 UTC：持续验收已改为独立accuracy-first-v3账本，旧v2失败窗不改；超20秒仍统计但不单独作为不合格条件。已添加实际临时PostgreSQL90秒检测年龄后检索、绑定、有效当前报价录入，以及停盘/失效租约/删除消息拒绝测试；既有20秒实现测试仅通过显式legacy patch保留历史机制回归，不代表生产默认策略。首轮测试因新样本SQL引号语法错误退出1（.runtime/test-evidence/769202），已修正并冻结重跑，失败证据保留。

15:54 UTC：第二轮197项1失败（.runtime/test-evidence/772442）是旧provider退避测试仍要求超过20秒变终态；依新需求改为90秒检测年龄后保留60秒退避、无虚假成功/信号。冻结原命令完整重跑197项、0跳过、48.267秒、退出0（.runtime/test-evidence/776931，.runtime/review-accuracy-first-final.log）。命令：sh infra/test/run.sh test_review_accuracy_first test_review_detection_deadline test_review_scheduler_deadlines test_review_deadline_receipt test_recovery_deadline test_review_worker test_review_tool_evidence test_consecutive_windows test_window_audit test_audit_runner test_expert_iteration_supervisor。

当前阶段：隔离验证通过，正在固定父镜像逐项构建并发布API/local-mcp/worker，不变更模型、并发、600秒任务资源保护、数据结构或交易许可。旧数据不重审，持续观察采用新验收文件与最终发布后repair标记。
