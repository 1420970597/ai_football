# E295 Telegram 消息连接阶段恢复

更新时间：2026-11-05 07:28 UTC。状态：设计已记录，生产未改。

目标：修复 E293 自然流量中 POST events 首次 ConnectTimeout 导致单源暂时 unavailable 的可恢复路径；不把应用重试当成网络根因修复。

责任目录：services/collector/、tests/。前置：保持 E288 同 owner 租约恢复及现有事件锁、持久化回执、版本冲突/去重契约；不修改 E292 冻结候选。

证据：E293 07:17:23.323015650Z events/no_response/ConnectTimeout/3295.6ms，07:18:09 独立源状态 unavailable，后续正常；对应 API events 无失败日志，不等于已证明远端网络根因。桥另有 observed 一次及 fixtures 两次下游 BrokenPipeError，须单列，不能说整桥全成功。

缺口：当前只有 claim 的首次 ConnectTimeout 可重试，events 只对明确503重试。未连接成功的事件可用同一消息锁和完全相同请求恢复，当前需要等待整个源退避/补偿。

设计：POST events 和 claim 首次 ConnectTimeout 最多重试一次，原有0.5秒退避/原调用方整体截止继续生效，持有同一消息锁与同一请求体；ReadTimeout、PoolTimeout、ConnectError、PUT以及第二次失败不扩大策略。不提前确认检查点，不新增内存已处理项，不吞异常或隐藏健康故障。

实现动作：提取明确连接重试路径常量；补充事件相同正文/图片引用恢复、第二次失败、取消释放消息锁、同消息竞争串行、真实隔离PG一次持久化与一次审核入队测试；更新旧禁止events ConnectTimeout测试为禁止模糊失败及未授权路径。

验收：定向行为测试和冻结完整套件通过；受控仅collector发布，六源/租约及证据保护核验；之后完整30分钟自然监控保留实际异常和回执结果。无自然重现时只可称隔离恢复通过，不可称网络根因解决。

失败处理：保留原异常/失败退出码，候选不通过不发布；部署失败回退本项目collector镜像，原远端事件台账不回滚。不增加任何上游权限、登录或交易动作。

07:30 UTC：候选代码已写。首次定向64项44.368秒失败1（测试误把重复投递回执也视为必含review_id；实际契约只在首次建任务返回该字段）。保留e295-targeted.log/exit1，按真实重复回执契约改为核对数据库原任务编号，重跑64项44.408秒实际exit0。新增5项覆盖同体图片引用、失败可见、总截止取消、同消息版本不能越过退避、真实PG重复消息/任务均唯一且检查点未提前推进。完整冻结套件开始，期间不改候选源码。

07:45 UTC 完整冻结通过：候选3b27da4eb94a95d697a82a8b87cceacb4c7b066a，1368项630.549秒actual exit0，0失败/错误/跳过、committed_source_verified=true。证据.runtime/e295-candidate/.runtime/test-evidence/2666350/backend-tests.json，SHA a238fc1e6f1b5827bf722b51b71b9214225a4079475377a4456855f4527fc47d。原生产仍E288，待E294完整截止后仅collector发布；没有宣称自然故障恢复已验收。

08:11 UTC 单服务发布通过：E294完整截止后，08:05:16.169555732仅collector切33d7958ebb04431236640ad8570b663e239aa9920bdc8c662fe1764b41348466，新容器1b652a28462147ba2a06bb5b981d3b8ad7eadc86f0174f379ee9738fb7c655b9。旧90秒租约自然交接期间HTTPStatusError保留，08:07:25六源无错误，随后连续两次ready。apply实际exit0，其他远端服务ID、环境/挂载和七本地服务均相同。267原推荐/报价及409旧ledger哈希全部保留，保护核验0。首次只读rootfs docker cp失败1，后改公开脚本通过python -c传入、镜像用stdin预加载成功，未修改容器文件系统。原16文件源码精确核验，非15文件。后续新30分钟才验收自然恢复，不声称网络根因已解决。
