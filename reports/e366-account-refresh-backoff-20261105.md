# E366 账户后台刷新失败退避

更新时间2026-11-05 21:52 UTC。状态：设计已记录。目标停止已失效刷新在每个dispatch周期重复访问上游，减少共享API/PG负载；责任services/api、tests，前置E361桥10秒超时和E365只读任务终态证据。

当前证据：21:18:51、21:19:11、21:19:29、21:19:45、21:19:59连续refresh任务均account_http_rejected。finish已保留任务failed，但没有更新renewal_retry_after；dispatch仍把last_verified_at旧且health_state=healthy的账户每十秒重新排队。执行器已有同一finish的幂等重试，这不处理新任务重复。未查明具体503对应任务，保留这个限制，不将相邻终态冒称精确关联。

设计：仅后台refresh/reconcile的failed终态设置最少60秒冷却，使用已有renewal_retry_after，若原冷却更长则保留。禁止改变账户身份、凭据、会话、健康值或资金许可；失败不是登录过期证据，不自动续期。成功校验现有路径清除冷却；unknown/rejected沿现有语义。相同finish重试先走幂等返回，不延长冷却。无schema变更。

实现动作：在已锁本人账户的finish事务内写失败冷却；加入隔离数据库的连续dispatch不排队、冷却过后重排、成功清除、长冷却不缩短、重复finish不延长、verify/unknown隔离测试；检查原账户绑定/租约/越权/回执测试，再冻结全套、同源镜像、仅API部署与真实只读观察。失败则不部署并保留原记录，修正后重跑原检查。

验收范围仅重复刷新负载，不能称上游403已修复，也不能替代专家3秒/官方规则/稳定窗口验收。当前E363/E364观察不中断。

21:54 UTC：最小实现及四项行为测试已写，尚未验收。首轮测试命令未设置本模块专用ACCOUNT_TEST_DATABASE_URL，45项跳过，exit0明确无效；02使用临时Docker内具名account_state_test角色，66项/74.097秒actual1，唯一错误为新reconcile测试误用jobs别名NameError，非业务失败。修正为既有account_jobs后重跑原66项；不将跳过或失败写成通过。a101d0e推送完成并远端完整引用一致0。

原检查03实际0：66项/74.007秒，失败、错误、跳过均0，覆盖账户协议原契约及新增四项冷却行为。临时Docker数据库和专用角色/内部网络，无生产连接；运行期间源码冻结。失败refresh/reconcile保持原账户和会话，dispatch等待到期，成功后清除，相同finish不延时、冲突finish409、另一用户账户不变、verify/unknown不误转后台重试。准备源码阶段提交、干净工作树全套及同源候选，尚未部署。
