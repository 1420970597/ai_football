# 账户隔离测试数据库清理

更新时间：2026-11-07 12:07 UTC。状态：代码已写、定向及最终冻结全回归隔离验证通过；发布后的自然观察继续。

## 工作包

- 目标：消除测试夹具在连接池关闭后仍申请跨角色强制终止权限的清理竞态，同时保留连接泄漏失败。
- 责任目录：tests；仅测试夹具与验证证据，不修改业务权限、数据库迁移或生产配置。
- 前置依赖：任务专用Docker PostgreSQL16、account_state_test建库角色、冻结验证镜像；不得指向生产。
- 当前证据：E670冻结1681项/1151.032秒，断言失败0、清理错误1、原退出1、OOMfalse。失败测试为test_existing_conflicting_stage_rejects_authorization_without_mutating_order；共享夹具关闭pool后执行DROP DATABASE WITH (FORCE)，收到InsufficientPrivilegeError。PG原日志11:23:59.399 UTC与残留临时库一致。原候选构建门禁退出1，未构建。
- 缺口：原瞬时后台进程已退出，无法从结束后的pg_stat_activity确认该次进程身份，不把autovacuum推测写成已证实事实。最终合并冻结全回归尚未完成，不可据定向通过发布。
- 设计决策与实现：pool.close之后使用普通DROP DATABASE。它无需主动申请跨角色终止；未关闭客户端会使清理明确失败。仍保留finally关闭管理连接；不授予pg_signal_backend，不屏蔽异常，不关闭autovacuum，不重试掩盖原失败。
- 验收：清理前恢复原pool并关闭两个HTTP客户端；pool关闭后才连接管理库；精确执行普通DROP；连接占用与权限错误原样抛出且管理连接关闭。既有账户和订单阶段证据全部重跑。
- 失败处理：保留原日志、退出码和临时库证据；最终回归任何错误继续阻止镜像门禁及发布，调查后重新冻结，不改正在运行的被测树。

## 已执行验证

1. `bash .runtime/e209-live/e687-target.sh`：冻结目标源码，任务专用Docker，账户夹具、账户行为、订单阶段证据三模块，59项/84.326秒，原测试及控制器退出0。新增3项明确验证清理顺序、连接占用错误不被吞掉、权限错误不被吞掉。
2. `docker run ... python /probe.py`，脚本为受限运行目录的e688-cleanup-ownership.py：只使用E670任务临时PG中新建、具名的测试库。另一个postgres角色客户端连接占用时，WITH FORCE重现权限拒绝；普通DROP返回ObjectInUseError且客户端SELECT1仍成功；关闭该连接后普通DROP成功。四个条件全真、原退出0，临时库已删除。未修改任何角色权限。
3. E670原日志、状态和失败码保留；E681仍在其原始干净冻结树上运行，未被上述测试修改。
4. 后续E681原冻结1693项原退出0；包含本夹具修复的6badb01最终E690冻结1696项/1153.589秒（报告整体1157.570秒），原退出0、失败/错误/跳过全0、无OOM。没有通过增加角色权限、屏蔽异常或关闭后台任务获得通过。

参考代码核验：PostgreSQL REL_16_STABLE的procarray.c中TerminateOtherDBBackends进行跨角色终止检查，CountOtherDBBackends另行处理普通DROP的后台进程等待。此次原进程身份未保留，因此参考源码只解释两种清理方式的权限差异，不替代实际现场证据。
