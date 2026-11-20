# 盈利专家模式的全量CI兼容修复

更新时间：2026-11-09。状态：修复及冻结定向验证通过，待父代理阶段提交与完整远程CI复验；业务与生产未修改。

- 目标：修复 dfe2cdd 全量远程CI引入的13个失败，同时维持迁移并发、索引身份、原始证据及无资金专家模式验收强度。
- 责任目录：仅 tests 迁移/CLI测试与本独立记录；不修改业务、主进度、工作流、父代理冻结副本或生产。
- 前置依赖：父代理继续TG/盘口与生产监控，原远程失败日志保留；全部Python与数据库验证在Docker，使用独立临时库。
- 当前证据：PR78的 Platform verification backend 失败，浏览器/Android/contracts通过；GitHub运行42594864241、提交dfe2cdda90f97eea62a0376c3d3d5b97b28ce21e，2000项、773.756秒、13 failures、退出1。
- 根因：12个旧升级测试将待应用迁移尾部硬编码截止029，添加030后真实返回包含030；1个CLI测试仅继承环境变量而未将 frozen_suite.py 在进程内注入的sys.path传给新Python进程，因此app模块无法定位。原101项定向验证设置的外部PYTHONPATH掩盖了后一差异，不能替代全量CI。
- 设计决策：升级结果仍按完整有序列表精确比较，但以明确测试迁移文件为锚点，检查当前仓库声明的全部待应用尾部；锚点缺失直接失败，不按数据库返回值生成预期。不删索引OID/relfilenode、错误预建拒绝、执行计划、并发单次登记、重复执行与旧行/xmin/ctid不变的断言。CLI子进程从测试实际__file__定位冻结源码，显式设置三项模块路径与cwd；增加继承环境PYTHONPATH被污染的回归。
- 实现动作：共享迁移尾部测试辅助函数，更新六个失败迁移模块的精确期望；修复CLI测试启动参数；通过真实frozen_suite入口选择全部失败模块及原101项相关模块运行，不提供可用的源码PYTHONPATH，仅保留镜像默认的空/workspace路径，以复现CI进程边界。
- 验收场景：13个原失败全部恢复；同名错误索引依旧拒绝、预建索引不重建、迁移事务/并发/重复登记、证据不变；CLI在无继承路径/污染路径下预览、0600、禁止覆盖、应用重放、撤销均正常；新模式、统计重置与原账户/结算共同行为无回归。
- 失败处理：保留原日志及每轮退出状态，不能删断言、跳过失败、改业务或设置生产假数据。仅将隔离通过交父代理提交与全量CI复验。

失败分布：message-event索引2项，通用迁移3项，quote-closure索引2项，quote-summary索引2项，latest-review索引2项，TG reconciliation迁移1项，statistics-cycle CLI1项，共13项。

研究命令：git status/log、13个Traceback与全部涉及测试只读检查，退出0；gh auth status/pr view与run view退出0，确认PR78及失败提交；旧gh不支持checks --json，退出1后改普通gh pr checks，成功读取backend失败与其余通过（该命令按失败状态退出1）。修复后git diff --check退出0。

下一步：新私有冻结，在固定验证镜像.25CPU/384MiB运行真实frozen_suite入口的定向模块集合；获取容器真实ExitCode与机器报告，不仅依赖日志OK。父代理e2215/e2228冻结与生产未触碰。

冻结验证已启动：私有归档SHA256 `edc0b18459b6b46244fd7a95697a6e19428a302f5d1f08bd1f514afdb532dc72`，使用固定验证镜像a8fc0cb5…，具名容器 `ai-football-profitable-ci-regression`，.25CPU/384MiB、源码只读、/tmp限制192MiB，连接已有隔离PG网络。实际入口 `python /verification/run_ci_regression.py` 仅临时选择12个相关模块，调用未修改的 `/source/infra/test/frozen_suite.py` 完成拷贝、sys.path注入和机器报告；测试的__file__位于真实/tmp/frozen树。新增CLI污染PYTHONPATH回归也在此入口执行。初次归档包含规划中尚未建立的assets目录而退出2，确认实际目录后仅归档已有职责目录重新冻结成功，无业务或原冻结副本修改。

最终结果：具名容器原生 exited / ExitCode=0 / OOMKilled=false；真实frozen_suite报告171项、0 failures、0 errors、0 skipped、260.120秒（含发现/导入，unittest执行254.499秒）。集合包含13个原失败、全部失败模块的其余正/负测试、原101项相关功能及新增污染继承路径测试。该次验证恢复了先前失败场景，并继续覆盖索引身份/物理节点、并发登记、错误定义拒绝、证据/账本不变、动态盈利筛选、统计重置、账户/订单与未知结果。保留 `ci-fix/original-remote-failure.log`、`final-ci-regression.log`、`final-ci-container.json` 和 `evidence/backend-tests.json`，最终源码未在测试期间更改。

最终检查：7项测试文件SHA256清单核对通过、git diff --check退出0，清单SHA256 `ce67bf52bc480421f584937fb7f775f9673c25c72e66e70dbd00ce1f3a346660`。仅7测试文件及本记录有本包改动，未改业务、工作流或主进度；无提交、推送、生产迁移、统计重置或资金请求。远程CI状态仍为原dfe2cdd失败，需父代理提交后重新运行完整验收，不能将本次171项局部通过称为远程CI已绿。
