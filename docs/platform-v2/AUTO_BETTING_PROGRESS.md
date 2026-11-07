# 用户无法开启自动投注修复

当前状态（2026-11-07 03:14 UTC）：用户已明确授权切换已验收执行器，并在本人当前专家选择、金额、限额和账户许可下恢复后续真实自动投注。候选livequote-2f76369于2026-11-07 01:47:25 UTC已切换，账户门内保全本人许可、未补投历史、未发测试下注；自然账户刷新继续成功。03:10只读复查账户healthy/ready、session/consent/会员有效、auto=true/emergency=false，策略已由本人于03:01:19更新为revision14、允许1位专家，存在同时间user/auto_betting_authorized审计；保留本人最新设置，不还原交接时revision13的3位专家。新策略生效以来没有允许专家新信号，尚无自然成交证据。无需再次申请相同切换授权。详见本文末尾授权和切换证据，以及reports/e484-authorized-executor-cutover-20261107.md。下列2026-11-04至11-06“未授权/未部署”表述仅为历史阶段记录，不代表当前状态。

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
13:33 UTC实时采集器检查：控制服务connected/authorized，Telegram 6来源connected；Leyu `partial`，274次轮询/20次失败，最近轮询39.5秒，完整盘口95/96，活跃报价46557条但15秒新鲜0条，最新约34.6秒；WebSocket有事件但重连19次。当前滚球信号因此全部不满足fresh_quote，未生成订单。状态：外部行情新鲜度依赖受阻，不能通过放宽阈值或赛前下注掩盖。

## 2026-11-06 投注时实时赔率匹配

15:45 UTC工作包建立：目标是专家只负责比赛、玩法、期间、盘口线和方向匹配；投注执行时读取乐鱼当前赔率和选项编号，不将专家倍率或数据库参考报价作为价格锁。责任目录`services/api`、`services/executor`、`tests`；设计、验收及失败处理见`AUTO_BETTING_REALTIME_QUOTE.md`。旧逻辑在信号预留时同时要求数据库赔率新鲜且等于专家倍率，执行器之后又按该快照赔率和旧选项编号查限额；本轮实现已写，代码已冻结进入隔离验证。

协议和盘口线Docker定向回归：`docker run ... python -m unittest -q test_account_protocol test_line_contract`第一次退出1，暴露两项旧断言仍把参考价精度及盘口不匹配后的无关并行请求当作契约；修正断言后同命令33项退出0。首次账户状态回归49项退出1，包含新快照字段未投影进协议对象、stage测试模块搜索路径错误和一条实时赔率断言未通过；已修复执行对象投影、测试PYTHONPATH及选项轮换/API授权测试。第二次账户状态与订单阶段56项仅一项Decimal尾零字符串断言失败，改为数值比较后第三次原组测试56项110.092秒退出0。全部定向共89项通过，日志`.runtime/auto-betting-investigation-20261106/{protocol-test.log,realtime-state-3.log}`；请求使用mock HTTP和临时Docker PostgreSQL。

生产只读核验显示API`LIVE_ORDERS_ENABLED=true`；既有运行记录显示用户本人自动投注许可已开启。由于用户此前明确表示实际交易尚未授权，本轮不把候选部署到当前可实盘服务，不发起真实订单。隔离回归通过后继续做干净冻结全套、同源候选构建、Git提交及远端引用核验；线上切换需要独立实盘授权。

15:48 UTC核心执行修复定向验收通过：专家倍率1.95、参考行情过期/关闭，但执行器从乐鱼当前盘口读到2.35及轮换选项编号`option-current`；当前赔率用于限额查询、授权、订单和不可变阶段证据。当前market/play/line/direction任何不匹配仍失败关闭；API校验15秒报价时间和每项本人授权，未知接单继续查单不重发。

16:09 UTC初次全量冻结与候选通过：干净worktree提交`6683b5e0ee6465d973da42d1106e850f9a2393c2`运行全套1592项退出0；之后只修正`quote_observation.py`一条过时注释，未改业务逻辑。为精确绑定最终提交重新冻结。

16:28 UTC最终冻结与候选完成：最终提交`2f763690fc5a6c087f8912fd4210e038a3ed9200`的干净worktree运行`sh infra/test/run.sh`，退出0；机器报告1592项、774.610秒、失败0/错误0/跳过0、`committed_source_verified=true`、恢复差异为空，报告SHA256 `773bd2bd183f188e94fcee1ab8164a235c1e815660eb4d51f134cbd7f0325534`。候选API镜像`ai-football-api:2.0.0-dev-livequote-2f76369` ID `sha256:98acc3c5d1b318490595b5f384835051e59a7083ad743c5a4200a00912dfbd08`；executor镜像`ai-football-executor:2.0.0-dev-livequote-2f76369` ID `sha256:11c02130c317cdee8436581faf914db11876521ea9cbc1ca628faa83af6e02db`，完整OCI revision均匹配最终提交。两镜像容器导入检查和最终`sh infra/remote/check-compose.sh`均退出0；Compose报告4项配置通过、started=false、secrets_used=false。最终提交已推送至当前分支。

部署状态：未替换生产服务。生产API`LIVE_ORDERS_ENABLED=true`且本人策略开启；切换会让之后的真实推荐进入可下单链，用户此前已明确实际交易未授权。不得以当前没有待执行信号或隔离通过推断获得资金授权。保留候选镜像和提交供后续实盘授权后切换；本次未修改个人策略、实盘开关、生产账户或下单数据。

### 2026-11-07 01:42 UTC — 已获后续真实自动投注切换授权

用户已回复“授权切换，恢复现有许可下的后续真实自动投注”。该授权取代此前未获真实交易切换授权的阶段状态；范围仅为已验收执行器切换、沿用本人现有专家选择、金额、限额和账户许可后的新推荐，不补投历史推荐，不以真实下注做测试。禁止修改个人许可来扩大授权。执行前重新只读核验账户、活动订单与部署基线，并使用真实空闲门及明确的新推荐边界；原暂存脚本中的文件标记不能替代实际锁与状态核验。当前状态：切换准备中，尚未宣称恢复成功。

01:44 UTC切换设计：责任目录为部署与运维证据，本轮不再改已验收执行逻辑。01:43真实只读检查退出0：本人策略revision13、3位允许专家、绑定许可/会员/限额有效，账户healthy/ready，资金订单0、五分钟派发候选0；旧远端执行器仍autobet401，运行开关true、重启0。采用限时数据库事务锁住现有账户，确认无运行账户任务、资金在途/未知订单及旧五分钟候选，再顺序替换单个执行器；门内核对策略指纹、切换前推荐仍无可派发项，其它远端服务不变后释放。新推荐可在交接期间排队，旧推荐不补投。任一核验失败，在门内恢复原执行器，释放锁并保留失败证据；标记文件不单独作为持锁凭据。原全套1592和同源镜像证据保持，部署不发测试资金请求。

### 2026-11-07 01:48 UTC — 现有许可下的新执行器已切换

授权后的受控交接完成，controller、remote apply、账户门原生退出均0。01:47:13.914882 UTC取得真实账户行锁，确认资金任务0、旧五分钟候选0；门内切换前后策略指纹d374a477…一致，既有数据库事务排空，切换前候选仍0。远端仅executor重建为已验收livequote-2f76369（镜像ID sha256:11c02130c317cdee8436581faf914db11876521ea9cbc1ca628faa83af6e02db），运行健康、实盘开关仍true，collector和api-bridge实例及启动时间保全，Compose其它服务hash不变。23个复制源文件及requirements和1592项完整报告在同源容器重新核验0；未改应用代码、个人许可、专家、金额或限额，未发送测试下注。门内复核通过后显式释放，后续新推荐按既有5分钟资格、当前真实盘口和本人风控正常处理；没有补投历史。状态：部署切换与配置/权限保全已验收，真实成交未作为测试，继续观察自然账户刷新及后续真实推荐。
