# 新增 Telegram 频道监控生效修复

更新时间：2026-11-13 23:07 UTC（柏林 2026-11-14）。

## 当前阶段：隔离验证通过，准备真实联调与部署

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
