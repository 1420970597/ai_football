# E358 完整检索解析读取合并

更新时间2026-11-05 20:36 UTC。状态：研究中。目标：减少每次独立resolve_match的一次远端数据库往返，保持原模型/提示及全部完整分页、归属和解析拒绝契约。责任目录services/api、tests。

前置与证据：原解析在同一事务中先读取当前attempt搜索complete，再读同一job/search_key候选，两个串行远端语句；已保留真实解析2.8–4.5秒样本。模型自己可能省略快速绑定字段且系统提示尚不能改变，服务端可等价减少固定开销。

设计：一条LEFT JOIN读取指定job/attempt/search_key的搜索状态及其全部不可变候选；只有存在complete=true的当前attempt搜索才能继续。空完整搜索返回空候选并明确no_matching_fixture；不存在、跨attempt或未完整仍409，不能把缺候选当完整。后续双方唯一核验、绑定写入、当前phase读取与工具快照顺序保持。

实施：只合并两读，不增加缓存，不复用模型自判结果，不改变分页/阶段/赔率/权限；新增SQL调用结构及零候选、旧attempt测试，重跑原模型基线、完整搜索直连、跨任务及工具证据回放。

验收：独立resolve路径快照查询一次且返回一致；正常、空、歧义、未完整、跨attempt、停用及完场场景通过；冻结全套0失败错误跳过；同源候选与生产基线兼容后受控上线，真实消息另窗观察。SQL次数减少不是三秒验收数据。

失败处理：保留原检查与日志；任何快照/版本/权限或回放失败不发布，修正后重跑原测试；无真实新推荐不称业务验收完成。

20:40 UTC隔离验证通过：bash .runtime/e209-live/e358-targeted-tests.sh，Docker临时内部网络/PG与0.5CPU/512MiB测试器，81项92.511秒、失败/错误/跳过0、actual退出0。覆盖真实API+PG独立解析六SQL/精确快照回放、完整空搜索、跨attempt拒绝，以及原完整分页/权限/完场/模型持久化基线。SYSTEM_PROMPT及其现有摘要保持逐字节不变。准备可审查阶段提交、干净冻结全套与同源候选，生产尚未更新E358。

21:03 UTC冻结验证通过：源码6a4993848aecc790306a530c1f5ed1dd87cdbb16、committed_source_verified=true，1477项/718.068秒（机器720.658）、失败/错误/跳过0、actual0；报告.runtime/e358-candidate/.runtime/test-evidence/177815/backend-tests.json，SHA54bfe695d0ce19123cbc73e17d0e38830ecbfc2fc795a0ce7efbdc27f4f241aa。构建镜像sha256:e8769fcd406c9edf4384a62f584c8c45b3d744c2ab5d0d97f222e2ca3eb30962；96源码/依赖精确核验0、10001读取93源码及28迁移并导入API/worker/migrate0（首次命令漏PYTHONPATH失败保留，03重跑通过）。Compose预检仅API镜像变更0，不改worker及其他服务。下一阶段自然空闲门内API单服务切换、schema029/函数/不可变推荐与账本核验及双端真实浏览器；没有变更迁移或生产模型提示。

21:12 UTC生产切换及页面验证：自然门21:05:44暂停新领取/21:05:47自然空闲，bash .runtime/e209-live/e358-apply.sh actual0，仅API更新e8769fcd，其他六服务容器ID/启动时间、全部环境/挂载/资源/用户/安全摘要保持。API schema029及两精确有效索引、合并快照实际函数、原prompt摘要及摘要查询逻辑均核验0；386原信号及报价、492原ledger完整保留0，无DB回滚；门已释放。无迁移、无worker重建、无模型配置修改。

bash .runtime/e209-live/e358-browser-checks.sh actual0，真实Chromium1440/390各五状态十点击全部200，选中状态与数据phase一致；六原文详情场景同attempt双回执、5推荐卡数及授权原图decode成功、无JS错误/弹层水平溢出。诊断初载/刷新双端全200、数据库reachable/schema verified，刷新4862/3664ms，仍接近预算，不称三秒。证据e358-phase/natural/diagnostics-browser-output及各exit0。E361切换后完整观察已启动但首样本状态异常，稳定验收继续未完成。
