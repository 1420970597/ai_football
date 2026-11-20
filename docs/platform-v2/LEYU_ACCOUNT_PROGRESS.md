# 个人乐鱼账户反复校验失败修复

更新时间：2026-11-04 21:09 UTC。当前目标：修复个人账户登录链路和失败后的可恢复性，并让页面自动显示最终校验结果。分支沿用 fix/TASK-78-hermes-telegram-sync；保留现有 PROGRESS.md 未提交更新及 reports/feijipan-share-20261031/，不混入本阶段提交。主进度近期存在另一项桥接调查，采用本独立进度页避免覆盖其记录。全部运行、验证在Docker内进行，限制资源，不触发实盘。

## 工作包

| 功能 | 责任目录 | 状态 | 依赖与现有证据 | 缺口、设计、验收及失败处理 |
| --- | --- | --- | --- | --- |
| 登录代理及App拒绝定位 | services/collector、services/executor、reports | 隔离验证通过；外部依赖受阻 | 单次真实6026明确新设备挑战；本人确认官方授权安全码；原生authCode字段核实，协议已部署；代理新租约CONNECT200，410间歇原因不猜测 | 已实现单次授权码与稳定设备；真实个人会话和余额等待本人页面提交有效码，未知不重试，不能冒称真实联调通过 |
| 账户任务结果和恢复入口 | services/api、tests | 隔离验证通过；运行核验通过 | 数字码、发送状态、绑定版本历史、冷却和未知结果保护均通过完整1295项；线上当前6026恢复动作已核验 | 保留原失败记录；对真实恢复只接受上游会话与余额证据；不扩大为实盘验收 |
| 账户页自动更新和中文诊断 | web/app、tests | 隔离验证通过；运行资产核验通过 | 1440/390真实浏览器含安全码提交与自动最终状态通过，无资金调用；线上资产与冻结提交哈希一致 | 有界轮询不覆盖编辑，离开/隐藏停止；真实本人新码成功后再核对线上最终展示，当前尚无成功余额 |

## 研究阶段记录

- 20:10 UTC：git status/log/tail、旧记录和配置只读核对。当前本人与采集账户隔离仍有效，个人名称/密码/设备材料存在；IPRoyal代理无既存session/lifetime后缀，启用默认短期模板。两条6026及410未变化。docker exec API只读查询exit0；shell首轮误读不存在tests/run.sh导致组合exit2，随后按实际infra/test/run.sh读取exit0。
- 已读取reverse-engineer-anything与playwright技能；REA会话未打开目标，工具可用，保留当前无目标状态。官方代理文档web检索两轮均工具HTTP502，尚未据此推测410原因；继续通过Docker内官方站点读取与已有证据。
- 下一步：只读核对提供方文档/官方App已有静态证据，执行单次有界代理连通探针（不登录）；先记录设计和原失败测试，再实现并冻结验证。尚未修改应用或运行配置，不把隔离通过当真实个人登录通过。

20:24 UTC设计与代理探针：采用现有任务JSONB保存数字码/发送状态，不新增数据库迁移；公众只返回有限诊断字段及绑定版本，不返回内部details/凭据。界面当前绑定与历史任务分开并显示时间；轮询只更新展示、不刷新编辑中的设置表单，离开/隐藏页面暂停。用户校验与后台调度遵守已有续期冷却，unknown不自动解锁。代码开始实现，尚未验证。远端现有执行器内一次相同新租约出口探测+目标CONNECT为200、6321.27ms，未发送App登录；证明410非每次必现，不能固定归因凭据。第一次临时Docker探针漏覆盖镜像ENTRYPOINT，启动了本任务临时隧道而未执行脚本，已定向停止该临时容器（exit0）；使用--entrypoint python重跑exit0。官方文档容器直连两次ConnectError，REA仅检查一个既有派生APK静态元数据（ev_50a34ae51c8fc8a5e2b3ad7a16ee0f6068310a78d05376c4207c0183b87774af，2.0.2），未执行目标；后续不再启动宿主分析引擎，沿用Docker内已有静态证据核对。缺口仍为6026解释，下一步在有持久租约的个人任务内进行一次无资金联调，不旁路重复提交密码。

20:21 UTC真实根因确认：账户本人确认官方端可正常登录。使用目标账户事务锁创建且原子领取单次verify诊断任务，写入user_requested_recurrent_login_repair审计；复用原远端执行器协议与API租约、续期、结束回执，不领取/查单/发送资金订单，正常exit0。上游6026的去敏中文解释为“因为您本次在新设备登录，为了您的账号安全，需要进行身份验证。”因此不是已证实的密码错误，而是本人账户新设备身份验证。当前绑定实现每次生成新UUID，加剧重复挑战；下一步稳定同一身份设备、按官方协议研究验证入口，不更换身份或规避上游验证。首次时间20:24为记录时估计，实际设计/探针在本条之前完成，以命令UTC输出及审计时间为准。

20:31 UTC隔离验证阶段：旧版本加新行为用例3项3.316秒exit1（设备重绑变化、任务缺数字码、冷却仍返回202），保留before-tests.log。候选冻结副本中账户/协议/续期84项46.724秒exit0；同身份改密码保留设备、不同身份新设备、410数字码不丢且无私有details泄漏、6026专用验证提示、冷却和后台调度均通过。前端当前任务有界轮询只改展示，正在编辑的账户与限额不重填，离开/隐藏停止、失败最多3次重试；双端真实浏览器场景已写，准备冻结验证。官方App静态研究改在Docker内CPU .5/768MiB、禁网执行，整类反编译耗时过长已定向停止本任务容器，改为有限方法/字段读取；还不能声称已实现官方新设备验证码协议。原生产仍未部署本账户候选，最新真实任务拒绝保持不重试，自动投注关闭。

20:51 UTC授权协议设计与实现：本人补充确认使用乐鱼 App 下发的授权安全码。Docker 内直接使用已安装 dexlib2/baksmali 有限读取，沿 DeviceVerifyViewModel$requestLogin$1 → LoginRepositoryImpl.d → LoginDataSource.i 核实原设备授权码映射 authCode，短信为 sendCodeNum，邮箱为 eCode；未猜测或使用短信发送接口。官方元数据 getLoginAuthCode 单次只读返回6000、空数据结构，未获取/发送验证码、未登录。新增本人授权码提交入口，只接受当前绑定已确认6026或该授权任务失败后的恢复；未知结果仍阻断。安全码加密暂存于5分钟任务，领取后数据库去除密文、执行器单次消费，禁止写入个人永久会话/审计/公共接口；重绑/解绑删除待用码。账户设备保持不变、自动许可关闭、只进行无资金校验。

验证证据补充：双端浏览器第一轮exit0（1440/390、自动最终状态、编辑内容保留、无资金调用），产物候选.runtime/web-integration-1001401。新增实际Mock ReadTimeout回归后85项48.189秒exit0。第三轮85项49.514秒业务断言通过但临时库DROP遇InsufficientPrivilegeError，专用临时角色补pg_signal_backend后原检查85项49.913秒exit0。第五轮89项52.641秒exit1：新测试错误假设要求会话内二次认证，而项目已明确取消该规则；实现沿用有效本人会话正确，改用撤销会话401验证，不新增认证门槛。原检查正在第六轮重跑；授权码双端真实浏览器验收同步在冻结副本中进行。Docker静态读取首次缺--user0得到PermissionError，定向重跑exit0；不掩盖失败。当前生产尚未部署，真实账户仍等待本人安全码，不发送重复密码请求。

20:54 UTC协议与账户冻结回归通过：`docker run --rm --name ai-football-account-fix-targeted --cpus=.5 --memory=512m --network ai-football-account-fix-test ... python -m unittest tests.test_account_state tests.test_account_protocol tests.test_leyu_session -v` 第六轮89项62.410秒exit0；证据.runtime/leyu-account-fix-20261104/targeted-tests-06.log。新增安全码本人归属、有效会话、未知/冷却拒绝、加密/不公开、领取后删除、过期后新码恢复、authCode与短信/邮箱字段分离均通过。状态：代码已写、针对性隔离验证通过；双端完整浏览器仍运行，真实联调仍待有效本人安全码。准备可审查代码阶段提交，再在对应干净提交副本执行全套后端验证。

20:56 UTC代码阶段已提交并推送168252e，远端引用核对待末次汇总；保留另一工作包PROGRESS.md修改。安全码双端完整浏览器第二轮exit0，证据.runtime/leyu-account-candidate/.runtime/web-integration-1069548，替身verify4/refresh1/查单回执1，transaction_calls0、failures[]。对应干净提交隔离工作树.runtime/leyu-account-release执行`sh infra/test/run.sh`，全套后端运行中；API/web/executor三个同源码候选镜像构建exit0。远端只加载执行器候选镜像exit0，尚未重启服务；其image_id为196b3a5657bc56251135d179c8da2420f2729c9507ce7ed3e5ecedef0a29e596。已按实际容器标签重建当前完整Compose覆盖链与环境文件引用，config --quiet exit0，不回退现有配置。等待全套结果后按空闲门禁更新API与本人执行器、最后网页。

2026-11-04 21:04 UTC完整冻结通过／部署阶段开始：干净提交168252ef6f3c6a0124d9e7c94d35e571ad2b6513执行`sh infra/test/run.sh`，1295项678.517秒，exit0，失败0/错误0/跳过0；机器证据.runtime/leyu-account-release/.runtime/test-evidence/1080200/backend-tests.json（envelope耗时682.651秒，committed_source_verified=true）。恢复difference={}；测试预算.75CPU/512MiB、专用PG.5CPU/384MiB；既有构建器核验.5CPU/384MiB，构建顺序执行。浏览器与发布副本涉及文件cmp一致exit0；三镜像ID记录image-manifest.txt。生产账户当前reject且active_account_jobs0，保持原设备与自动关闭。准备以有界空闲门禁仅切换API，再更新本项目远端executor，最后web；review-worker、collector、bridge和持久数据不在本次重启范围。阶段时间以实际命令UTC输出为准，前文手工时间为阶段记录近似值。

2026-11-04 21:09 UTC运行切换与核验完成（本人授权待联调）：

- 空闲门禁`bash .runtime/leyu-account-fix-20261104/apply.sh gate < .../infra/remote/review_handoff.py`：21:04:43暂停新审核领取、21:04:46取得空闲锁；API切换exit0并healthy后立即release，日志确认gate_released=true。不删除Compose警告中的历史孤儿容器。
- `bash .../apply.sh api`、远端受限Docker SSH部署helper、`bash .../apply.sh web`均exit0；API启动21:05:04.683Z，web启动21:07:01.798Z。review-worker维持既有e241及20:52:04.693Z启动时刻，本次未重启。远端只`up --no-deps executor`，实际镜像ID一致且EXECUTOR_LIVE_ORDERS_ENABLED=false；collector/bridge不在本次命令范围。
- 三镜像版本2.0.0-dev-account-168252e（开发候选）：API image_id 0133c061bd2119b8853bdd9a01c00e2a1b4fcea37b88134b9106cddc67f9b8ef；web 602412d7e587e38c116d561939c6c1405a41bb291267b56d48704e50c83d4677；executor 196b3a5657bc56251135d179c8da2420f2729c9507ce7ed3e5ecedef0a29e596。均源自168252e，不代表正式Release/APK验收。
- 旧API丢失本次诊断业务码，按本次唯一account_diagnostic_authorized审计job_id和当前绑定4严格匹配，只补该真实任务provider_code6026与login_submitted=true；新增account_diagnostic_code_recovered审计。exit0/rows1，未改账户健康、设备、凭据、余额或成功时间，未再次登录，不改旧历史失败。
- 运行postflight第一轮exit1：诊断脚本误假设FastAPI include_router立即展开app.routes；实际为_IncludedRouter，独立router与OpenAPI已含新路径。仅修正诊断为OpenAPI路径检查，原检查重跑exit0。`docker exec -i ai-football-v2-api-1 python - < .../postflight_live.py`确认healthz/readyz200、新路径存在、游客提交401、同身份稳定设备代码存在；当前绑定4、provider_code6026、recovery_action=provider_identity_verification、私有details未公开、成功时间/余额仍不存在、自动关闭且紧急停止。证据postflight-account.json。
- web正在服务的index.html与app.js SHA256逐项等于冻结源码（add970bc49a444f74dc2ee9552789edf0d9c9db5700277c11b3348297df07ae0 / d9401000628b8c312b8b4e8d7e6aa9235063431f626a3461f82b3b152e09944d），HTTP资产核对exit0。
- 源码提交168252ef6f3c6a0124d9e7c94d35e571ad2b6513已push，ls-remote精确一致exit0。当前等待本人在已上线页面提交有效官方授权安全码；已明确不要在聊天中发送。完整任务未宣称真实个人登录通过，不再重复密码尝试。
