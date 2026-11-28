# E220 Telegram事务长尾定位

更新时间：2026-11-04 13:39 UTC。状态：研究中，线上镜像与E219观察配置冻结。

目标：依据真实分阶段证据定位采集接口503和长事务，减少源消息进入持久队列前的等待，避免无依据扩容或超时放宽。责任目录services/api、services/collector、tests、docs/platform-v2、reports。依赖E219完整窗、现有远端数据库只读诊断权限和受控预算。

当前证据：E217完整日志3次503，但只保存了聚合结果，旧容器重建后不能恢复那三次详细堆栈，根因尚未确认；不得将其全部归因CPU或模型。E219新窗前65个请求无失败，连接池等待P95≤0.1ms，reconciliation事务P954230.0ms、live源消息锁阶段P952849.9/max3315.4ms、事件SQL P95765.3ms；普通跨远端请求往返约255ms。delete单次7492.1ms事务不足以概括全部删除。阶段统计首轮正则把phases后的首指标误吞，已修复为明确字段匹配并重采，数据不作旧首轮完整分段依据。

缺口：尚无新503的具体异常类型/安全栈；慢source-message-lock是否同消息/来源锁等待，长reconciliation/delete是否行锁、计划或多次WAN往返，均需证据。不能通过移除源锁、去重/版本保护、事务或缩短保留窗口提高表面速度。

设计与动作：完整窗保持镜像/上下文/资源固定；以Docker只读脚本采样pg_stat_activity等待类别、事务年龄及阻塞数，仅允许项目application_name，不记录SQL文本、凭据或用户消息。API日志只提取operation/status/category/安全frames/耗时字段，保留失败但不输出原日志。定位后在独立候选将同一事务中的可合并往返合并，保留锁顺序/版本检查/回执原子性，先负向复现及正常/重复/乱序/删除/租约失效/断线恢复验证，再冻结回归及下一窗口部署。

验收：没有漏任务和未确认写入盲重试，消息及媒体证据不变，多群相互独立、删除与编辑一致；对原接口同等样本比较失败数及P95，模型和排队时间分别报告，不把采集毫秒数当端到端三秒。诊断无权限或失败时记录缺口，不修改数据库权限/生产配置或编造根因。下一步只读等待采样与新窗失败实时保留，当前尚无实现改动。

13:50研究进展与候选设计：首轮完整37次/189.0秒采样后段发现3条阻塞（2transactionid、1advisory），最长事务7.118秒；较早22样本无阻塞不能代表全窗，已向用户更正。新窗API安全栈实际LockNotAvailableError出现在resolve_match写入比赛绑定；同期事件接口182次均200，不能把该MCP503算成事件接口失败。第二轮采样只在数据库端将当前语句归为明确操作类别并返回类别/阻塞方，不记录或回传SQL正文；用于定位事务与绑定是否相关，尚未获得因果证明。

代码核对发现结算持有比赛FOR UPDATE直至提交，投影对每条推荐依次insert-ledger/read-ledger/update-signal，N条需要1+3N次远端往返；这会阻塞其他事务对比赛的外键检查，是可明确减少的持锁开销，但不能声称已证实是上述唯一根因。决定在独立候选批量插入台账、一次读取校验整批不可变证据、批量更新信号，保持原算法、锁顺序、旧created_at选择及409冲突规则，约固定4次往返，不改变数据库结构/结算口径/订单权限。

先写行为负向用例：真实隔离Postgres，8条推荐模拟每次往返150ms，结算持比赛行锁期间并发建立审核比赛绑定，要求在既有1.5秒锁预算内成功；旧逐条实现应复现锁超时，批量后通过。另覆盖整批重放时间不漂移、单条不可变冲突使整批回滚、无信号/待结算及原native方向。冻结后运行原结果台账定向及完整套件，不在E219完整观察中发布。失败保留样本，不放宽锁超时以求通过。

13:58候选与定向证据：新负向用例前两轮因测试建job遗漏idempotency_key、UUID/text同参数类型推断分别error/exit1，修复测试建模后第三轮在旧结算上真实LockNotAvailableError，5.219秒/1failure/exit1，不把建模error算成业务复现。独立候选41f37e7（根任务分支cherry-pick为a448c17）批量insert-ledger、整批read/validate、批量update-signal，含初始行锁读取共4次往返；保持同一算法、计算内容、Decimal、created_at及409冲突，未改迁移/数据库超时。冻结后`test_result_ledger test_settlement`共37项35.222秒exit0，原并发绑定预算用例通过，候选未部署。完整隔离套件同冻结41f37e7正在运行，源码不再修改。

第二轮37次189秒操作类别采样未遇阻塞，不能据此撤销首轮3个真实等待。新窗另一条真实多场推荐10个有UTC范围查询共108候选（原文时间为范围基准），最终仍出现timeout，日期范围不是完整延迟解决方案；状态继续跟踪。线上原图/同attempt回执1440/390共4点击场景复测exit0。一次脱敏intent读取因0600被非root测试镜像拒绝，保持原权限、改任务容器user0只读成功；一次统计内联语法错误已改为独立脚本重跑，不作为业务验证通过。Hermes源码只读查验时系统python缺yaml，未安装或修改全局依赖/生产reasoning参数，未将该诊断当成功。

完整回归（14:07 UTC）：同冻结41f37e75dfd0924bdfe846fe7fc4dc680b8aee67独立worktree运行infra/test/run.sh，1249项549.958秒、0失败/错误/跳过、exit0，机器报告SHAca287ca96b927e9bebb565bf12594949189b479be5c654e9eb5c92bec07905e4，隔离备份恢复业务差异空；套件中gate日志仅临时测试DB。API镜像ai-football-api:2.0.0-dev-e220-41f37e7 buildx --load exit0，revision记录候选短SHA41f37e7对应上述完整SHA，不与根分支cherry-pick混淆。E219完整窗证据已保存，准备仅API/worker idle切换，MCP/web/Hermes上下文与资源不变；改正上阶段健康检查假定，worker检查Running、API实际ready，不再把缺少Health字段视作有健康检查。配置解析exit0，部署尚待gate。

14:16实际生效：14:06:57.328暂停claims，等待原审核正常结束到14:12:55.500取得idle，仅API/worker切换为候选镜像sha256:7b7d83860823ade92a83c5439222a336a2fa72835901a09092c4a54105af392a，命令exit0/API healthy+真实ready200、worker Running且0重启，trap正常gate_released。MCP/web/Hermes及上下文/资源保持E219版本。正常受认证正式重算接口200再次处理原15场；只读核验38推荐/报价哈希、38结果、80台账版本与created_at全部相同，退出0（首命令漏Docker -i未执行脚本且无输出，不算通过，补-i重跑保存机器JSON才计证据）。这是持锁开销优化，不新增收益或算法版本。

新完整监控14:14:00.737690开始，计划14:44:01以后终审；首样本health/ready200、TG connected/authorized为true且六租约有效，但state=unavailable，与布尔状态冲突，必须在终审单列，不能仅凭旧watch的布尔healthy字段认定全正常。等待完整消息、接口失败及锁等待证据，暂不声称线上锁超时已消失或三秒达标。任务独立PG/验证网络在测试完成后按归属清理，不动其他项目。

## 14:14–14:44 完整真实冻结窗口终审

2026-11-04 14:14:00.737690–14:44:03.495827 UTC，1802.9秒/55样本，监控exit0，原始JSONL SHA0e768f91a5261a01e38cdbd44b3aab90b2ae7cc59576aade75c2da5e3f7a8665。health/ready全部200、6群租约始终有效单owner、无sample_error；首样本state=unavailable但connected/authorized=true，旧监控末行telegram_all_healthy=true未检查state，终审新增独立异常表，不据布尔值判健康。只读精确截点10自然帖/8eligible，1succeeded/6unresolved/1queued，7当前回执、0漏job、无截断；另2帖unsupported未进入模型，不混为已审核。自然图片大3.75实际接受1条，模型176146.744ms、事件至结论220101.886ms，3秒失败。

完整时间有界API日志统计exit0：live164×200、reconciliation322×200+1×503、delete22×200、unclassified8×200；全路由3个LockNotAvailableError、13个TimeoutError，不能归入全部采集接口或因采样200忽略。12当期事件版本接收入库P9510050.4ms、仅3个≤3秒，这不是模型端到端。窗口稳定性与延迟验收失败，已恢复未通过状态；不以7个回执称全部专家完成。原始明细/日志脱敏阶段统计与只读逐帖审计保存在任务运行证据目录。

下一阶段E222固定候选已通过1251完整测试，14:45:10暂停新claim等待自然idle，仅API/worker部署；E223补采锁序已独立PG复现死锁，单例修复通过、30API用例46.547秒exit0，完整冻结套件进行中，不混入E222。模型固定deepseek-v4.1-flash，不改推理参数，不捏造未知滚球计分规则。
