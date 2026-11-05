# E267：真实数据库超时预算诊断

更新于2026-11-05 02:20 UTC。状态：完整组合冻结验证通过，候选镜像已构建，尚未生产发布。

目标是区分生产Timeout到底消耗在预备语句还是实际执行等待，避免继续凭ClientRead猜测传输或CPU。责任目录services/api与tests，前置证据为E264未复现和真实asyncpg代码：_do_execute在_get_statement之后扣除准备耗时，再把剩余预算交给executor。现有日志只有代码位置与后端PID，不能区分两个阶段。

设计只对TimeoutError检查最多64个traceback帧，限定asyncpg实际Connection和驱动代码模块、已准备stmt及有限数值变量，输出最多4个固定字段的数字预算条目；不访问SQL、参数、异常文字，不调用未知对象。用父层原始timeout与驱动当前timeout、before/after重建准备与执行毫秒，条件不足返回none。保留现有PID关联和用户503响应，不加请求、不修改截止/重试/事务，也不把阶段判断当根因结论。

验收采用具名临时PG：真实慢执行得到execution等待；仅测试内延迟_get_statement证明预算在执行前耗尽；无关异常、伪装连接/帧、不完整数值、诊断失败不泄密且不覆盖原异常。原数据库HTTP/锁/回滚/恢复及完整冻结回归必须通过。独立候选实现不触碰正在冻结的E266，也不在E265窗口中改变生产。发布后需真实超时数字及新完整观察，未取得真实样本不声称解决超时。

候选实现使用驱动方法的实际code对象身份及具体Connection类型，而非仅信任模块名；只读取原始timeout及before/after、是否已准备stmt，跳过递归schema重试以免错误归因。日志保留PID最后一项，用户响应无变化。真实临时PG定向10项6.483秒exit0，含暖缓存pg_sleep慢执行30ms预算与仅测试包装准备阶段60ms延迟/20ms预算耗尽，均得到正确阶段，后续SELECT恢复；原HTTP、锁、回滚、恢复一起通过。无网络错误日志测试3项0.002秒exit0，伪装对象未被调用、异常值未泄露。生产尚无此代码，下一步冻结完整套件。

组合候选f70a69f6d67b24f6f3f39754ebad3ec3dd12895a（包含ea6fc83诊断与E266配置修复）完整1332项613.950秒、报告616.446秒，actual exit0、0failure/error/skip、源码核验true。证据 `.runtime/e267-candidate/.runtime/test-evidence/2007367/backend-tests.json` SHAd5e3c510f6c1e96af5507f125d57edce68500c2a2e4412ea62f1f69840481c4c。候选镜像ai-football-api:2.0.0-dev-e267-f70a69f构建exit0，IDf9ca590c、revision标签同完整源码；未部署。下一步只更新API（worker不调用该HTTP诊断入口），保持其他服务及数据，在E270完整终审之后再发布并开新完整观察。准备E270终审脚本初次误用候选工作目录导致四个路径不存在，被后续build的0遮盖；已保留失败并在正确根目录set-e严格重跑准备exit0，不将原组合命令当全部成功。

02:45 UTC：API-only部署准备检查实际exit0，确认候选干净、完整套件1332、镜像精确ID及revision、合并配置有效。E270仍在进行，不提前切换。部署脚本仅替换API，持有自然idle门锁并在退出时释放，失败恢复E250；前后比较其余六服务身份/启动/镜像/资源/规范化环境摘要，以及API资源和环境摘要。准备完成不等于已经发布。
