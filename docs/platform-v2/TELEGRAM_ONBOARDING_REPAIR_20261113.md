# 新增 Telegram 频道监控生效修复

更新时间：2026-11-14 00:07 UTC（柏林 2026-11-14）。

## 当前阶段：监控修复发布验收通过，历史补收持续推进

- 目标：管理员新增频道后，明确完成来源保存、真实频道类型核验、发布身份授权、运行时加载、实时及补收持久化，不再以“已保存”冒充“监控中”。群组继续按明确作者授权；保留撤销、原始证据和 20 秒审核上限。
- 责任目录：API 来源管理与采集协议、采集器运行周期、管理页面、针对性测试、部署及本记录。
- 前置依赖：已授权 Telegram 会话、持久数据库、来源账户实际可访问；应用与验证全部在 Docker 中执行。
- 当前证据：create_source 仅写来源，不建立频道自身发布身份映射；页面却按 enabled 直接显示“监控中”。来源刷新位于最多 120 秒历史轮次前，新增配置与租约刷新可能受历史工作拖延。正在读取真实新增来源、身份映射及运行状态。
- 缺口：尚未核验本次新增来源的身份、实时收取、租约及历史限流情况；未写代码、未发布。
- 设计决策：先获取真实证据再定修改；不依据负数 ID 或昵称猜测频道类型，不自动重新启用撤销的作者，不新增平行信任体系。
- 实现动作：诊断新增流程与采集运行时；补全明确授权的频道接入与可观测状态；增加失败回归、隔离验证和真实只读联调；按必要服务顺序部署。
- 验收场景：真实广播频道、普通群组、无个人 sender_id、撤销映射、来源启停与热添加、历史限流时实时处理、多频道同时消息、数据库持久化与审核入队。不能以在线连接代替来源生效验证。
- 失败处理：保留原失败记录，定位后重跑；生产不写测试消息、不实盘投注、不回滚既有业务数据。
- 已执行：Git 工作树检查，退出码 0；读取来源创建、采集周期及页面保存路径，退出码 0；生产容器与日志快照已保存至项目私有运行目录。业务树仅预存 .omc/ 未跟踪；根树既有 CI 修改及无关文件保留。
- 下一步：读取生产来源映射与运行回执，确认具体断点并编写针对性失败测试。

## 阶段证据

- 生产只读基线：2026-11-13 22:55:07 UTC，17 个来源。所有已识别广播频道已有频道发布身份；其中 1 个没有事件记录，需用真实最新发言时间与作者匹配继续区分静默和漏收。另有 1 个群组无信任作者，不得自动信任群内所有人。
- 15 分钟采集日志至少 330 次 GetHistoryRequest FloodWait，证明后台读取过频，不能把连接成功视为来源健康。
- 变更：明确勾选广播频道授权后，服务端用当前会话真实类型核验再原子创建发布身份映射；群组仍需选择作者；增加显式频道授权与监控回执接口。配置与租约每 5 秒独立刷新（有效租约 30 秒更新一次），后台历史读取全账号限额并遵守 FloodWait；实时事件不等待该后台预算。
- 首次诊断命令失败保留：docker cp 因只读 rootfs 退出 1；改为 docker exec -i Python 标准输入。首次只读 SQL 引用了不存在的 created_at，退出 1；按既有 updated_at 修正，复跑退出 0，未修改数据库结构。
- 第一轮隔离命令：sh infra/test/run.sh test_telegram_sources test_telegram_channel_identity test_telegram_source_scheduling test_telegram_recent_recovery test_telegram_version_conflict test_collector_lifecycle test_collector_quote_policy；114 项，47.066 秒，1 失败，退出 1。失败是原测试仅重置单频道退避，未模拟全账号退避到期；修订测试同时核验共享限流恢复，生产限流不缩短。
- 尚未发布；下一步补齐新行为测试，冻结代码后完整回归与浏览器验收。

## 冻结验证

- 完整目标范围为 24 个模块，覆盖来源、身份、正文/相册、编辑删除、租约、去重、连接恢复、媒体、保留期和审核入队。最终命令为 `sh infra/test/run.sh test_telegram_onboarding test_telegram_sources test_telegram_channel_identity test_telegram_source_scheduling test_telegram_recent_recovery test_telegram_version_conflict test_telegram_collector test_telegram_api test_telegram_reconciliation_claims test_telegram_receipts test_telegram_author_before_album test_telegram_detection_timing test_telegram_reply_lookup_failure test_telegram_media_scheduling test_raw_retention test_telegram_directory test_telegram_profile_batch test_telegram_entity_cache test_telegram_event_connect_recovery test_telegram_lease_connect_recovery test_telegram_lease_release test_telegram_session_identity test_collector_lifecycle test_collector_quote_policy`。证据 `.runtime/test-evidence/3212892/backend-tests.json`：405 项，64.101 秒，零失败、错误或跳过，退出 0。
- 中间失败保留于项目私有运行目录：433 项有 2 失败、1 错误，为历史无限页请求断言、旧单来源 FloodWait 假设及串行预算后错误测量历史 RPC 并发；按新明确契约修订测试（限流期间仍实测其他来源实时事件正常入库）。405 项第二次有 1 错误，是测试并发计数器放错函数；已更正并重跑上述全部模块，未调整生产限额绕过测试。
- 浏览器命令：限额 `.5 CPU / 768 MiB` 的 `ai-football-browser:v2.0.0-dev` 容器执行 `tests/browser/telegram-channel-identity.mjs`，网络 none；退出 0。1440/390 两种宽度均通过显式身份选择、新频道默认核验授权、等待加载与生效回执、核验失败不伪造添加成功、无横向溢出；无浏览器脚本异常。截图与 JSON 位于 `.runtime/onboarding-browser/`，已检查桌面与手机截图。
- 未更改数据库结构、审核 20 秒上限、历史新鲜度门禁和投注许可；无真实投注或生产测试消息。
- 下一步：提交验证通过的源码；按当前生产镜像做最小叠加构建，核对配置、资源与挂载，再顺序发布 API、采集器、网页。随后核对 17 个真实来源、最新头部发言、持久入库和限流变化。

## 发布阶段

- 源码提交 `60021e281019b40675f9adade497054ac565f1d1`，已推送 `fix/TASK-79-channel-identity`，Git push 退出 0。保留未跟踪 `.omc/`，未触碰根树既有 CI 修改。
- 8 个变更运行文件对冻结测试文件哈希逐个核验。按当前真实运行镜像叠加构建 API、采集器、网页；无依赖升级。构建命令使用 Docker `--network none --cpu-period=100000 --cpu-quota=30000 --memory=256m`，退出 0。首次私有构建准备容器因默认 UID 无权读取 0700 私有目录退出 2，改用该任务容器 UID 0，不放宽私有目录权限，复跑退出 0。
- 预检确认环境变量、命令、挂载、网络、端口和资源预算保持一致；API .4 CPU / 768 MiB，采集器 .5 CPU / 512 MiB，网页 .1 CPU / 128 MiB。无数据库结构变更。
- API 镜像 `sha256:e1fa48ae01273ee5d7013cbb8f302691edf02b30c4b3de495e0cc66132d84475`；采集器 `sha256:f2c8f827fa65a653379aad9a794eef95a7f1b6a8bc6286b48470cbaf1edd7927`；网页 `sha256:098d1ebf2edf1728becaa08e8293f4d88014c1f0570ee400f1245908194914dc`。网页生成新内容版本目录，保留旧版本资源供已打开页面读取。
- 发布前只读确认审核租约空闲及运行镜像/配置未漂移，按 API → 就绪检查 → 采集器 → 网页顺序发布；2026-11-13 23:09 UTC 完成。已有审核工作器和其他服务未重启，未创建消息或真实投注。
- 下一步：观察真实来源头部发言、信任匹配和对应持久事件，确认后台限流降幅；归档生产验收与正式编排顺序。

## 生产观察后的补充修复

- 第一版 23:09 至 23:50 UTC 真实观察：实时事件成功暂存 68 次、暂存失败 0；后台预算 1033 次读取、FloodWait 0。仍有后台核对超时，日志显示共享预算排队错误地计入 5 秒 Telegram RPC 超时，现将预算等待置于 RPC 子截止外，24 秒核对总截止仍保留。
- 1 个新增广播来源已有明确频道信任但头部作者不匹配，不能直接认定缺少个人 ID。核对 Telegram 官方 message.post 与 channels.toggleSignatures 契约：广播帖可包含签名个人或其他发布身份。新增兼容仅用于真实广播类型已核验、频道自身已明确授权且 post=true 的帖子；已有显式个人映射优先，群组与普通转发消息不能获得该权限。官方参考：https://core.telegram.org/constructor/message ，https://core.telegram.org/method/channels.toggleSignatures 。
- 增加只返回消息类型、是否有文图、发布时间、授权匹配和匿名化身份类别的内部只读检查，不回传原文或原始发送人 ID。用于区分正常帖子与服务通知。
- 补充 4 项针对性行为测试，重新冻结后执行前述全部 24 模块：409 项，64.022 秒，零失败/错误/跳过，退出 0；证据 `.runtime/test-evidence/3258781/backend-tests.json`。
- 下一步：将补充修复仅发布 API 与采集器；网页保持已验证版本。继续核对新增来源真实消息与持久事件，不以事件总数冒充推荐总数。

- 补充修复提交 `85cf28a57b9d18d639ba5b55d45724aa09ba26eb` 已推送；API/采集器于 23:52 UTC 发布。23:54 只读探针显示 17 个来源全部加载、后台状态 degraded=false，47 次共享读取且 FloodWait 0。
- 真实核验接口联调暴露方法误用：对字符串调用同步 `_group_entity`，应调用异步 `_entity` 获取并校验真实实体。旧模拟错误替换了该同步校验器，因此遗漏；现改为真实 TL Channel + DirectoryClient 实体缓存走完整解析，并覆盖 left=true 的拒绝路径，重新运行全套测试。该失败不隐藏，不以 409 当成功。
- 未入库的三个头部样本已初步分类：一个是群内未信任作者正常文字，不能自动收取；一个是没有文图的置顶服务通知，不应生成推荐；一个是已授权频道的签名普通文字，较早消息已越过 3 小时快速补收窗口，需按原始 72 小时保留范围定向补收，不能重置历史检测时间制造实时信号。

## 最终验收与剩余依赖

- 实体解析修复 `87f3b30c00befb5e14fc877a6c645b7ba90f61f5` 已推送，409 项重新冻结验证通过，63.208 秒，退出 0，证据 `.runtime/test-evidence/3327142/backend-tests.json`。仅采集器于 2026-11-13 23:56:02 UTC 再次发布；API 保持 `85cf28a5`、网页保持 `60021e28`。
- 当前镜像：API `sha256:4f68b3a80cbad7d96aa1b761214f8ded8d25e24ba610f46c0d24797d383b8136`；采集器 `sha256:1c21f2cec54d75b3812998ced074bea1af4ffe54a3f292a8769c8f587f3253ff`；网页 `sha256:098d1ebf2edf1728becaa08e8293f4d88014c1f0570ee400f1245908194914dc`。7 个 API/采集器变更运行文件与冻结源码 SHA 一致；网页新内容版本 `35eda09f...`，已核对生产文件含新表单及回执函数。
- 2026-11-14 00:01:16 UTC 最终只读验收：API ready=200，Telegram connected/authorized=true，17 个来源已加载、17 个有效租约；后台状态 14 caught_up、3 catching_up，无 degraded。最新采集器 142 次预算读取、FloodWait=0，实时暂存 2、失败 0。此前第一版 41 分钟观察已有 68 次实时暂存、失败 0。此计数是事件投递，不是推荐数。
- 2026-11-13 23:56:02 至最终日志：同版本冲突 0、FloodWait 0、实时投递失败 0、后台核对截止超时 0；启动时有 1 次历史切片截止，后续恢复，保留日志不隐藏。
- 三个疑似遗漏来源各检查最新 25 条真实消息：未授权群组作者不入库符合权限边界；置顶服务通知无文图，23 条正常消息已入库；签名广播频道有 24 条正常帖此前缺失，已确认类型与授权并启动近 72 小时有界定向补收，未重置来源高水位。
- 定向补收首次因独立脚本未配置 JSON 编解码导致审计写入失败，事务完整回滚；补齐与应用一致的 jsonb codec 后重跑，2026-11-13 23:57:56 UTC 审计与任务原子提交。至 00:01:16 UTC 已恢复 10 个事件版本、10 条消息投影，均 media ready，0 媒体待处理；审核均按原时间标记历史回放/未解决，不制造新鲜下注信号。历史游标 78662 → 78652，尚未到窗口底部，后台继续推进。
- 1 个启用群组仍没有信任作者，管理员页面明确显示“未生效：尚未授权发言身份”；用户需选择已核验作者，不能默认信任全群。现有审核 9 条检测截止超时等未解决结果原样保留，本次未证明所有专家在 20 秒内完成推荐；监控收取验收不能冒充解析准确率或收益验收。
- 发布清单 `infra/production/telegram-onboarding.json` 应放在累计覆盖最后，包含各镜像实际源码版本，既有首页/日期/去重/72小时保留修复继续保留。已同步修订旧累计镜像指针，防止覆盖顺序回退。无需数据库迁移，38 条已应用记录不变。无生产测试消息、无真实资金测试、无自动投注授权变化。
- 证据汇总：`reports/telegram-onboarding-repair-20261114.json`，私有现场数据位于项目 `.runtime/tg-onboarding-20261113/`。本阶段只完成 Telegram 接入与监控修复，非整个产品正式版本验收。

## 最近发言覆盖复核

- 2026-11-14 00:05:54 UTC，对先前疑似遗漏的 3 个来源再次各读取最新 25 条真实 Telegram 记录，并按来源和成员消息 ID 对照当前持久事件：签名广播频道 24 条正常文图帖子全部入库、遗漏 0；置顶通知所在群组有 6 条已授权正常消息，遗漏 0；另一个群组最近 25 条均不属于配置的信任作者，保持不采集，未冒充漏收修复。
- 随后只读验收中，签名频道累计恢复 47 个事件版本及 47 条消息投影，41 个 ready、5 个媒体待处理，其余为修订状态；历史审核 41 条均保留 historical_replay 未解决原因。游标继续推进至 78612，72 小时回补尚未完成，不能声称全量历史已恢复。
- 后台状态 15 caught_up、1 recent_deferred、1 catching_up；近期补收偶发延后仍可见。最终日志同版本冲突 0、FloodWait 0、实时投递失败 0、核对截止超时 0；启动历史切片超时 1。不得将历史媒体待处理或近期补收延后隐藏为全绿。
- 来源运维提交 `cc24ff1e923eaddd85cf33fea798be72c0d9afd1`、根树运维提交 `19f818f837a2344be4c703932ea40d6cd7ad92bb` 已推送并以远端引用核对；本次补充复核单独提交。根树原有 CI/HANDOFF 未提交差异及无关目录完整保留。
