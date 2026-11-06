# 用户无法开启自动投注修复

更新时间：2026-11-04 22:16 UTC。目标：消除已开放后台总开关但部署层仍阻断本人自动投注的冲突。沿用当前任务分支，不改其他工作包，不替用户设置专家/金额或开启个人许可，不以真实下注验证。

| 工作包 | 责任目录 | 状态 | 前置依赖与当前证据 | 缺口、决策、动作、验收与失败处理 |
| --- | --- | --- | --- | --- |
| 自动投注部署开关一致性 | infra、services/executor | 真实联调通过（无资金） | 线上三处运行开关true、后台总开关true，API健康/就绪200；账户healthy/ready、会员权益有效，个人许可仍关闭，资金订单0 | 默认仍关闭，本次用显式覆盖按桥→执行器→API切换，镜像保留；后续个人专家/金额/限额与启用由本人填写。无真实资金成交验收 |
| 协议桥实盘领取白名单 | infra/remote、tests | 真实联调通过（无资金） | 27项桥测试通过；真实executor令牌经bridge严格live_capable=true返回200/idle；候选源码SHA一致 | 默认拒绝、开关允许、collector越权、畸形query与非布尔拒绝均通过，认证及路径边界保留；短账户锁已显式释放，不以真实下注验证 |
| 本人开启与停止回归 | tests、web/app | 隔离验证通过 | 完整1306项零失败/错误/跳过，1440/390本人表单启停通过，替身交易发送0；线上本人许可未被代开 | 当前个人专家0/限额未填写，由本人按页面确认；真实资金行为及收益不属于本次验收 |

研究开始：git status/log退出0，当前仅保留未跟踪reports/feijipan-share-20261031/。`docker exec -i ai-football-v2-api-1 python - < .runtime/auto-betting-fix-20261104/diagnose.py`只读事务exit0，证据before.json不输出身份或秘密。源目录检索曾误用不存在tests/test_remote_bridge.py导致rg退出2，随后按rg --files定位真实测试。现有API拒绝保护按设计工作，根因是部署未开放，不通过删除业务门禁或伪造账户健康解决。下一步复核桥参数契约、提交规划、实现显式部署覆盖和测试，再冻结验收。

22:01 UTC设计与代码阶段：用户确认实际提示与线上API拒绝一致。远端只读清单exit0确认executor候选168252e仍live=false。新增真实HTTP桥回归先运行单项exit1（明确开关true仍403），原失败保留before-test.log。实现桥服务显式布尔开关，默认false；仅true时允许严格live_capable=true且保持executor令牌范围；远端Compose环境默认false可配置，新增本机API与远端桥/执行器配套opt-in覆盖文件，不改默认安全策略。运行tests.test_remote_deployment冻结模块回归中，未改生产配置；后续须检查现存账户/资金任务0和个人许可关闭后按序发布。

22:03 UTC针对性桥验证通过：`docker run ... --network none --cpus=.3 --memory=192m ... python -m unittest tests.test_remote_deployment -v` 27项11.967秒exit0。覆盖默认拒绝、显式开关允许、executor令牌、collector越权、畸形query和非布尔值拒绝、真实Unix桥传输、超时不重试、日志脱敏及恢复。新增双端本人表单保存启用/关闭回归。完整部署操作说明记录默认关闭与显式覆盖、按序切换和资金未知恢复，接下来提交冻结全套后端及浏览器；生产尚未改变。

22:04 UTC冻结验证进行：提交bdb41af已推送，独立干净worktree .runtime/auto-betting-release执行`sh infra/test/run.sh`与`sh tests/browser/integration-check.sh`，均Docker限资源、未改被测代码。线上只读资金预检exit0：enabled_user_policies0、active_orders0、active_account_jobs0、active_renewals0；未替用户启用。远端原桥SHA7838a1eb与Git父版本4e5ac9ea不同，已读取并容器内diff核对，只有末尾换行差异，保留原桥备份，候选有限差异均为本次显式开关。生产API当前e244、web仍账户修复候选，部署只改API环境并保留实际镜像，不回退新版本。

2026-11-04 22:05 UTC浏览器隔离通过：干净bdb41af的`sh tests/browser/integration-check.sh`exit0，证据.runtime/auto-betting-release/.runtime/web-integration-1321066，1440/390真实表单启用及关闭、无效限额拒绝、会员撤销、会话隔离、紧急停止与既有安全码流程通过，替身transaction_calls0、failures[]。线上全量覆盖链追加本机opt-in的`config --quiet`exit0，仅变环境、保留实际API镜像；新代码bdb41af push与远端引用核对exit0。完整后端仍运行。远端切换脚本准备就绪，先按实际容器标签逐服务保留原镜像/覆盖链，再新增桥及执行器显式环境，不重启collector或连接组件。

22:06 UTC远端候选已暂存（未重启）：受限Docker SSH helper exit0，原桥备份及原三个服务镜像/容器号记录在项目私有.runtime；候选SHA06f022cd15ccfd218f3f995bf67a144a6da7cc81db94a17d830bde06b7128257。部署将核对原桥与各服务无漂移，保留镜像只重建桥与executor；API最后开放。已准备真实领取只读验收：短事务锁住所有个人账户且确认无资金订单/个人许可，用现有executor令牌通过真实桥请求live_capable=true，预期idle，不允许返回资金任务；45秒上限并显式释放，避免测试创建资金订单。

2026-11-04 22:12 UTC完整冻结通过／切换开始：bdb41af干净提交`sh infra/test/run.sh`，1306项615.338秒exit0，失败0/错误0/跳过0；机器证据.runtime/auto-betting-release/.runtime/test-evidence/1320784/backend-tests.json确认committed_source_verified=true（envelope618.093秒），恢复difference={}。本机及远端完整Compose校验exit0；未改测试范围或默认检查。接下来最后只读预检为0后远端按桥→executor切换，再API空闲门禁更新环境；真实无资金probe与个人策略未被代开最后核验。

2026-11-04 22:16 UTC部署与真实无资金链路通过：

- 最后预检exit0：API false/后台true、个人启用0、活动账户任务0/续期0/资金订单0。`docker run ... AUTOBET_SCRIPT=remote_deploy.py ...`exit0，先api-bridge后executor重建，镜像均严格保留原ID；桥源码SHA06f022cd...，实际两个环境都true；collector容器ID及2026-11-03启动时间完全不变。分别启动22:12:57.452Z、22:13:11.045Z。
- API交接`bash .../apply-local.sh gate < .../review_handoff.py`exit0：22:13:29暂停新审核领取，22:13:33取得空闲锁。短账户事务确认无资金订单/启用许可并锁个人账户，远端真实executor令牌经实际bridge调用`POST /internal/executor/claim?live_capable=true`，HTTP200/kind idle/transaction_submitted=false，exit0；45秒上限锁显式释放，未领取或伪造任何资金任务。证据real-live-claim.json与account-probe-lock.log。
- `bash .../apply-local.sh deploy`exit0：仅追加API环境覆盖，当前e244-0525952实际镜像保持不变，API于22:14:41.555Z启动、healthy；review-worker仍21:44:35.171Z原实例，本次不重启，web/连接服务也不在本次命令范围。门禁release退出0、日志gate_released=true。
- 真实postflight exit0：api_live_enabled=true、global_auto_enabled=true、healthz/readyz200、enabled_user_policies0、active_orders0；记录automatic_betting_deployment_opened审计（user_policies_modified=false、test_transaction_submitted=false）。后续只读diagnose exit0：账户healthy/ready、本人会话与余额存在、会员权益true、最后22:15:20成功校验；仍个人许可关闭/紧急停止/专家0/限额未填写，不替用户作资金决定。
- 远端final inspect exit0确认桥和executor均running/live=true、实际Compose持久包含显式覆盖文件，源码SHA与冻结候选一致，collector未动。默认仓库配置仍false；本次线上通过单独覆盖显式开放，不能以默认false误判未生效。

状态：部署开关一致性真实联调通过（无资金）；协议桥隔离及真实领取通过；双端本人启停隔离通过。用户还需在页面本人选择专家、限额并确认启用，尚未替用户或用真实下注验收；赛前协议未核验仍不支持。源码bdb41af已push并远端引用一致；最终报告阶段准备提交。临时数据库和浏览器容器已由各自脚本正常清理，保留证据及其他工作包文件。早期手工阶段时间近似，以本段真实命令UTC输出为准。

## 2026-11-06 自动投注持续排查

阶段开始：当前目标是定位最新运行环境下本人启用、信号入队、执行器领取和预检回执链路的实际阻塞。责任目录services/api、services/executor、web/app与必要的infra配置；前置依赖为当前本人账户、会员、授权与报价，不能继承旧账户许可。当前证据：git status/log与docker ps均exit0；分支fix/TASK-78-hermes-telegram-sync，HEAD1789437，API候选e395、review-worker候选e399、web候选e327；其他审核工作包已有未提交修改，保留不动。Nov4部署开关修复仅为历史证据，不代表本轮自动投注已成功。计划先只读检查个人策略、账户时效、候选推荐、订单及远端执行器，根因明确后增加针对性修复与Docker验证；禁止用真实下单作为测试，禁止代选金额、专家或代开个人许可。失败保留原检查和退出码，修复后重跑。状态：研究中。

2026-11-06只读研究证据：本人策略enabled=true、emergency=false、已选专家2、绑定许可匹配、会员有效且限额正数；API/全局/远端bridge/executor四处开关均true，不再是Nov4部署开关问题。最后成功校验为2026-11-05 10:50:34 UTC，从10:51:48开始2997次account_http_rejected/401，最近至本轮05:03:57；订单0，24小时所选专家推荐14条均已完场结算。验证时效约18小时，超过资金预检120秒上限，不能伪造freshness通过。协议所有HTTP大于等于300统一为account_http_rejected，工作器仅对AuthExpired续期，因此HTTP401未进入续期路径；须继续读取上游脱敏响应确认401语义。诊断exit0；只读account_policy探测因行锁而被只读事务拒绝（ReadOnlySQLTransactionError），不作为业务失败证据，改查字段时效。远端清单exit0，executor仍Nov4候选168252e，collector由其他工作包Nov5更新，本轮保留。引用不存在的测试/发布路径引发exit2，已按实际目录定位。

2026-11-06设计与代码阶段：经同一远端executor单次本人余额只读请求（无登录、无交易）确认HTTP401、JSON code=0401013，属于既有AUTH_CODES明确过期，不是猜测状态码。协议先读限长响应，再仅对只读请求的401/403且明确AUTH_CODES抛AuthExpired；空/非法响应、权限码0401015、429、5xx均保留失败且不续期；submission分支仍未知且不重发。本人会话续期复用已有App token启动场馆，只有App token明确失效才走原登录流程，保留租约/冷却/拒绝/未知保护。新增协议回归、本人token重启且许可保留的工作器全链路回归、权限拒绝不续期回归。修复前独立worktree运行单项复现exit1、4个子场景同样错误，证据before-test.log。修复后测试源码冻结到独立worktree，开始Docker限资源临时库验证；未部署。

冻结验证进展：协议23项exit0、共用App续期27项exit0。账户状态47项首跑exit1，唯一错误为临时数据库清理时account_state_test缺少pg_signal_backend权限（DROP DATABASE WITH FORCE无法终止连接），非业务断言失败；不放宽应用校验，给本轮独立临时测试角色补充既有标准清理权限后原47项完整重跑，首次日志保留account-test-first.log。新增两个HTTP401恢复/403不续期场景业务断言首跑均通过。生产尚未修改。

2026-11-06隔离验证通过：协议23项0.629秒exit0，共用App续期27项0.377秒exit0；账户状态47项补齐临时角色清理权限后原命令完整重跑92.996秒exit0，无跳过，合计97项。修复前失败和首次测试环境清理失败均保留。覆盖正常HTTP401业务过期恢复、本人token复用不密码登录、配置版本增长与原个人策略逐字段保留、403权限不续期、429/5xx不误判、实盘发送HTTP401未知且单次、跨账户与未知结果恢复保护。源冻结在.runtime/auto-betting-401-release；期间未修改被测代码。线上资金预检exit0：个人启用1、活动账户任务0、续期0、资金订单0。下一阶段构建带源码提交标识镜像、传输校验、短账户空闲锁交接仅替换远端executor，随后只读余额与自然任务核验，不创建测试订单。状态：代码已写／隔离验证通过，未部署。

2026-11-06候选发布阶段开始：修复提交5e3f0f29a5dd5384c2103b6cd2f358dc1cb88f01已push并与远端refs一致（exit0）。独立worktree切到此提交且干净；期间仅此提交代码与测试进入候选，其他工作包文件保留。固定基础镜像、已有受限BuildKit（CPU0.5/内存384MiB）构建executor候选exit0；镜像ai-football-executor:2.0.0-dev-autobet401-5e3f0f2，ID sha256:13e47479eab72ede5528c95edf5dcf7618738b3b597bc5dd62f4ebfdcf6e8bf1，OCI源码标识匹配提交。首次构建命令因独立worktree内重复相对路径exit128，修正路径后成功；没有应用代码宿主执行。仅准备并传输镜像，未重启服务。

2026-11-06 05:19 UTC执行器候选部署exit0：镜像受保护SSH传输docker load后ID匹配，空闲账户交接锁确认running_jobs0、active_orders0、policy_modified=false。远端按实际Compose标签保留全部环境/覆盖链，追加本轮executor镜像覆盖，config校验通过，只替换executor；2026-11-06T05:19:05.552Z启动，新镜像ID与OCI源码匹配5e3f0f2，live_enabled=true。collector及api-bridge容器ID前后完全相同，本机API/web/review-worker没有被部署。短账户锁已请求显式释放，未发测试订单。发布前本人策略指纹3f820c1c42ad73a143aad59c1710bbc6c8f28b55c99303ee079f57cae1ae02f8，仍待验证自然余额刷新是否恢复及策略未变；不能先称实盘验收成功。

2026-11-06 05:20 UTC真实无资金恢复证据：交接锁显式释放exit0；新executor自然账户任务05:19:37取得本人续期租约，05:19:59.671557完成succeeded。只读postflight exit0，health=healthy、renewal=ready、config_revision由10增至12、校验年龄约5秒、资金预检120秒时效恢复。auto_enabled=true、emergency=false、绑定许可与会员有效，策略指纹与部署前逐字相同；没有代改专家、金额或许可。当前所选专家五分钟内pending推荐0、资金订单0，不能以此宣称已真实成交。远端进程只读摘要exit0，新镜像running=true、restarts0、错误类别空；collector/bridge未重启。下一步观察至少一个自然刷新周期并写根因报告，确认恢复持续。

2026-11-06 05:21 UTC阶段结束：第二个自然账户刷新05:21:05.654143再次succeeded，最终只读postflight exit0确认校验年龄约18秒、healthy/ready、auto_enabled=true、emergency=false、绑定许可和会员有效，个人策略指纹与发布前相同，配置版本稳定12。续期审计显示05:19:54成功且login_submitted=true：原App token亦过期后走既有受租约登录一次，并非测试交易。资金订单仍0，五分钟内所选专家pending推荐0；不追投旧推荐，不宣称真实成交已验收。根因与证据已写reports/auto-betting-session-expiry-20261106.md。状态：HTTP失效识别修复隔离验证通过，远端执行器部署及持续无资金账户刷新真实联调通过；实际资金成交未测试。最后动作：提交本轮报告与进度并核对远端，不包含其他工作包PROGRESS.md修改。
## 2026-11-06 自动投注再次排查

用户反馈自动投注仍失效。13:13-13:30 UTC只读检查：账户校验持续成功、个人策略开启且绑定匹配、会员和限额有效；executor `/dispatch`直接探测200，活动订单0。推荐对照发现最近滚球推荐盘口closed、价格1.78→3.50、报价约1549秒未更新；其他近期推荐为scheduled，当前执行协议不支持赛前。远端collector日志出现持续36-51秒Leyu轮询、`state=partial`及网络超时，无法提供15秒阈值内新报价。结论是当前无可执行信号，不通过放宽资格、伪造报价或真实下单“修复”。详细报告：reports/auto-betting-still-inactive-20261106.md。状态：外部行情依赖受阻，账户和执行器链路正常。
