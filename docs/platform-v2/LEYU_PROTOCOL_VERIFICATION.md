# 乐鱼协议、实时采集与续期验证

记录日期：2026-10-26。当前状态为隔离验证阶段；本文不代表真实登录、远端采集或生产验收成功。所有业务与测试运行于有资源上限的 Docker 容器。用户收紧范围后，所有主机文件访问仅限本项目，不继续读取外部配置、外部备份或连接旧 VPS。

## 证据来源与边界

先前获得授权时，曾通过只读 Docker 挂载分析旧备份的原始协议和两份真实 HTTP 响应。最初权限不足退出 1；随后解码长 base64 因换行失败退出 1；修复为只移除空白并保留严格字母表验证后，真实回放退出 0。该回放在工作范围收紧前完成；没有复制凭据或原始完整抓包到新仓库，也没有修改备份。

| 范围 | 实际回放结果 | 可证明内容 |
| --- | --- | --- |
| 原始完整赛程响应 | 1868 场、11 个体育项目、333 个联赛 | 赛程层级、项目与联赛归属、完整数量 |
| 一批比赛详情响应 | 12 场 | 真实批次结构、行级玩法定义、字典及列表盘口 |
| 指定比赛盘口 | 36 条，36 条具有源时间 | 报价编码、盘口线、期间、选项标识及源时间 |
| 指定比赛独赢 | 主胜 32、客胜 1.07、和局 8.5 | `ov / 100000` 与独立捕获值相符 |
| JSON 序列化 | 成功 | 日期和十进制值不会导致上传失败 |

原回放采用容器内内存读取压缩归档及其嵌套会话样本，不落地秘密。其外部挂载命令不再列为现行可执行步骤，避免违反新的目录边界。当前可复现行为用脱敏重建样本及仓库内 Git 历史核验；历史真实回放和当前隔离测试分别记述，不混为真实联调。

当前项目 Git 历史内仍可核验旧客户端的批量详情、WebSocket `C105/C2/C21` 周期快照、比分与完场状态处理。未找到可以证明官方确认赛果的独立接口，也未找到足以证明滚球让球及大小球比分基线的规则证据。因此比赛从列表消失不产生赛果；滚球报价的 `score_basis` 保留 `unknown`。

## 标准化契约

赛程 ID 使用来源与原始比赛编号组合；主客队、联赛和项目保持独立。赛前、滚球、完场、取消分别归一；未知源状态保留原始编码并禁止误映射为滚球。`mst` 按已过秒数或分钟时钟解析，`mmp` 是阶段，不能拿作分钟。

盘口包含 `kind`、`period`、十进制线、方向、系统价格、源时间、接收时间、选项状态、玩法编号、盘口实例编号、选项编号、主附线与比分基线。已核验种类为 `winner/handicap/total`，其余为 `unsupported`；期间为 `full_time/first_half`，未知保留来源编号。复合线计算均值，负号继承按数字处理，非法线不置零。

源时间只接受真实 `ctsp` 或已核验推送时间字段；未知为 null，`source_time_quality=unknown`，接收时间单独记录。推送没有选项状态时，不能凭价格变化复活停盘；已有投影状态可保留，新选项要求 HTTP 补盘。盘口或源时间无效不能成为可投注信号。

完整批次响应才标 `markets_complete=true`；单场缺失、缺少盘口容器和失败批次标 false。部分推送永远不关闭全部旧盘口。源时间较旧的事件不得覆盖新报价。明确赛前报价的比分基线为 `full_period`，其他报价为 `unknown`，不凭常见规则推定滚球结算。

## 续期与网络契约

业务 `requestId` 与 App token 分离。没有真实业务会话时禁止随机生成授权编号；旧 `leyu_api_token` 只能在 App 续期侧作为旧配置兼容项，不能发送为业务 Cookie。普通采集、动态域名发现、初始化与场馆启动均关闭环境代理继承。

只有明确的业务会话失效码或被授权的首次初始化才进入续期。网络超时、HTTP 403、权限拒绝和限流均不触发密码登录。先尝试有效 App token 启动场馆；只在明确 App token 失效或缺失后进行一次密码登录。上游拒绝或请求结果未知在远端持久阻断，管理员核对后才能重新授权。

续期代理仅在该登录事务中取得短租约，出口探测和登录使用同一短期连接身份。探测用于签名绑定，场馆和数据请求仍走真实网络。没有全局代理修改和固定出口。IPRoyal 的动态短会话与断线停止语法采用其[官方轮换文档](https://docs.iproyal.com/proxies/residential/proxy/rotation)；秒单位选 59 秒，并在本地预留安全余量，依据其[有效时长说明](https://help.iproyal.com/en/articles/7215287-how-long-does-a-sticky-ip-session-remain-the-same)。代理配置值、密码、token 和私有签名材料均不进入本文。

远端接口有采集服务鉴权、账号及全局配置互斥、租约、未知终态、冷却、配置版本 CAS、受限秘密字段、加密持久化和不含秘密的服务审计。更换账号哈希不能绕过同一全局采集身份的阻断状态。新 token 在场馆启动前先加密持久化，避免启动失败后丢失登录成功结果。

## 已执行的隔离验证

在代码冻结后执行以下命令。数据库被限制为本任务专用 Docker 主机、专用库和专用角色；错误连接在任何 DDL 前拒绝。测试用户口令仅用于此临时隔离库，与真实凭据无关。

```sh
docker run --rm --cpus=0.5 --memory=384m \
  --network ai-football-v2-test-net \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e LEYU_RENEWAL_TEST_DSN=postgresql://renewal_protocol_test:isolated-renewal-test-only@ai-football-v2-pgtest/renewal_protocol_test \
  -v /root/ai_football:/w:ro -w /w ai-football-api:v2.0.0-rc1 \
  python -m unittest discover -s tests -p 'test_leyu*.py' -v
```

首次完整合并验证：61 项，1.007 秒，退出码 0。覆盖 23 项协议、8 项 WebSocket、9 项隔离 PostgreSQL 续期、6 项运行适配和 15 项登录/代理/发现测试。包括 1868 个订阅不截断、45 场分批并发上限、部分失败、403 不登录、压缩样本、状态不复活、源时间未知、旧报价拒绝、5 个并发续期仅一项成功、配置热修改冲突、加密与审计脱敏、未知/拒绝重授权及登录代理隔离。

后续工作器整合曾执行 73 项测试，退出码 1。失败是独立任务测试在 Telegram 立即停止后，未等待乐鱼失败事件即检查状态；改为真实失败事件触发停止，保留原错误类别断言，73 项重跑 1.278 秒、退出码 0。另发现并修复 HTTP 库 INFO 日志可能包含代理提取查询密钥：仅主入口设置日志，HTTP 库级别为 WARNING。

最终冻结验证覆盖 78 项，1.691 秒、退出码 0，无跳过。采用同样的 Docker 限额、网络与只读本项目挂载，镜像改为 `ai-football-verifier:v2.0.0-dev`，命令如下。最后增加的情景涵盖远端撤销秘密不被本地默认值复活、过期成功回执变未知、管理员核对过期孤儿租约、明确授权的 App 身份在业务网关尚未建立时完成初始化，以及缺少状态/原始标识时禁止开放盘口。

```sh
docker run --rm --cpus=0.5 --memory=384m \
  --network ai-football-v2-test-net \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e LEYU_RENEWAL_TEST_DSN=postgresql://renewal_protocol_test:isolated-renewal-test-only@ai-football-v2-pgtest/renewal_protocol_test \
  -v /root/ai_football:/w:ro -w /w ai-football-verifier:v2.0.0-dev \
  python -c 'import unittest,sys; loader=unittest.TestLoader(); suite=unittest.TestSuite([loader.discover("tests",pattern="test_leyu*.py"),loader.discover("tests",pattern="test_collector_lifecycle.py")]); result=unittest.TextTestRunner(verbosity=1).run(suite); sys.exit(not result.wasSuccessful())'
```

测试通过只能证明对应隔离场景，不能证明外部服务接受当前身份、代理质量、生产数据新鲜度或正式版本可以发布。

## 长期工作器生命周期

### 本次采集恢复修复（2026-10-27）

运行时出现 `private_config_unreadable` 的原因是 Compose 以前把仅限 root 的 0600 签名与发现文件直接 bind mount 到 UID 10003 的 collector；文件权限无法由应用安全地绕过。现改为 Compose 顶层 `configs`，由 Compose 在启动时读取项目内运行时文件并以只读配置文件交给 collector 和 executor。Git 中的文件仍保持 0600，服务仍以非 root、只读根文件系统和 `no-new-privileges` 运行；API 不再获得这些配置。`docker compose ... config --quiet` 退出码 0，渲染结果确认 collector/executor 只有两个配置目标和 S3 CA bind，没有旧的签名/发现 bind。使用临时 Compose 配置以 UID 10003 读取同一配置的检查输出 UID 10003 及 JSON 开头，退出码 0，未输出配置值。

实时连接的 HTTP 握手 403 原来没有落入重连异常分支，导致 `InvalidStatus` 冒泡并终止工作器。现在只记录脱敏类别 `websocket_http_403`（未知状态为 `websocket_handshake_rejected`），进入既有指数退避并持续运行；响应头和响应体不会写入状态或日志。新增合成 403 握手测试验证连接器被调用、重连计数、脱敏错误类别和正常停止。

恢复修复验证命令（全部在 Docker 中）：

```sh
docker compose --env-file .runtime/compose-test.env config --quiet
docker build --no-cache --progress=plain -t ai-football-collector:test -f services/collector/Dockerfile .
docker run --rm --cpus=0.25 --memory=256m \
  -v /root/ai_football:/w:ro -w /w ai-football-verifier:v2.0.0-dev \
  python -m unittest tests.test_leyu_realtime tests.test_leyu_session -v
```

结果：Compose 校验退出码 0；collector 镜像构建退出码 0；25 个实时、协议、续期和代理测试通过，0.304 秒，退出码 0。另一次只读 collector 镜像 `compileall` 尝试因根文件系统不可写而退出 1（仅是工具试图创建 `/app/collector/__pycache__`，不是语法错误）；生产镜像本身按只读根文件系统运行，不能用该命令作为编译证据。

```mermaid
flowchart LR
  Start[采集工作器] --> TG[Telegram 长连接及控制服务]
  Start --> Poll[有预算 HTTP 轮询]
  Start --> WS[WebSocket 心跳及完整订阅]
  Poll --> Config[远端配置版本与续期保护]
  WS --> Normalize[源时间及盘口增量校验]
  Normalize -->|未知或丢失| Poll
  Normalize --> API[鉴权 API 分批事务写入]
  Poll --> API
  TG --> API
  API --> Store[远端数据库及对象存储]
```

三个任务分别退避。控制服务及 Telegram 客户端只创建一次，配置变更热替换，退出取消子任务并断开客户端和控制线程。WS 连接不继承代理，配置身份变更时排空重连，未知推送只设置 HTTP 补同步标志，避免阻塞心跳。至少两秒的补同步启动间隔阻止未知消息造成全站请求风暴；失败退避不能被推送绕过。

每批最多 50 场及 2000 条报价；单场超过 API 允许的 500 条报价时明确报预算不兼容，不静默截断后声称完整。队列、推送单包与 WebSocket 内部缓冲均有限额；投递失败保留同一批次重试，溢出或重连请求补同步。当前未测真实上游峰值，需在真实联调中测量这些预算是否合适。

健康状态区分最后上传成功、源报价新鲜度、完整盘口数量、未知源时间数量、WS 连接与投递故障。完整拉取成功但源报价陈旧时不会显示健康；Telegram 控制状态返回 `source_health`。缺少官方确认赛果协议继续显示未配置，不以比赛消失、推送断线或旧账户订单代替通用赛果确认。

## 待验收与下一步

- 采集工作器生命周期、实时与轮询兜底的隔离验证已完成，仍需 Docker 部署真实联调。
- 在用户允许的项目内配置来源齐备后，验证真实采集与无资金的授权续期；当前没有执行真实外部登录。
- 补充官方赛果确认及滚球期间/比分基线的证据，之前不确认结算。
- 验证当前私有签名与域名发现材料在容器挂载中的引用、权限和有效性；不把历史材料存在当成当前有效。

## 2026-10-27：用户指定的比赛与盘口 HTTPS 中转（E98）

新增独立热配置 leyu_collection_relay，管理页面沿用采集设置布局，可保存和清空；清空后恢复直连。原业务来源保存真实业务 HTTPS 域名，中转地址不覆盖原业务域名。API 及采集运行时均验证来源格式；续期更新业务域名和会话时保留中转配置。

实际路由为 https://fuck.5545.eu.cc/api.ky2hkka.com/yewu11/...。仅完整赛程 GET、批量盘口 POST 和单场补盘 POST 使用该前缀；方法、查询、JSON、原 Origin/Referer 和有效业务会话保持既有契约。App 登录口令、App 令牌、IPRoyal 登录代理、WS、余额及订单不经过采集中转。请求带 no-store/no-cache，禁止重定向，不在 403、429 或网络失败后切换直连重试，已有预算与冷却保留。中转服务能看到它转发的必要业务会话。

### 测量与边界

- 切换前直连持续采集成功，无最新拒绝或限流，不能认定服务器 IP 被封。中转无凭据返回 HTTP200 / 0401013，仅说明到达授权校验。
- 13:37:00 UTC 有界中转探针：2,069 场赛程、12/12 场盘口，2.497 秒，无回退、失败或限流；未提交登录或资金操作。
- 13:45:44 UTC 管理设置确认配置版本 7、采集回执 7、应用状态 applied。公开接口 HTTP200、122 场滚球足球、2,594 条摘要盘口、2.028 秒；该时点默认 15 秒门禁无 fresh 场次，继续如实标旧。
- 13:46:08 UTC collection_transport=https_relay：2,091 场赛程、96/96 完整盘口、零失败批次、无最新错误或限流。WS 仍直连，订阅 2,089 场，当前队列/丢弃为零；启动短窗口累计 8 次投递重试失败后恢复，已交付 184/186 个入队事件，累计失败不清零。
- Docker 定向回归 65 项 / 1.344 秒通过；最终冻结全量 483 项、unittest 183.501 秒、外围计时 185.380 秒，零失败/错误/跳过，退出码 0。覆盖原头/体路由、单场回退、非法 HTTPS 来源、302/403/429 不改路、失效热更新保留旧配置和保存/清空。
- Docker 浏览器 26 场景、Compose 契约、22 模块 AST、JavaScript 语法及 Git 差异检查通过，退出码 0。脱敏快照见 evidence/collection-relay/；本机冻结结果不冒称已提交内容的远端 CI。

回滚：管理员采集设置清空中转并保存，确认新版本已应用。业务域名和会话无需改写。中转失败显示原因并退避，不自动切换出口继续请求。

### 远端提交验证

PR #77 已于 13:53:10 UTC 合入 main，合并提交 5fc8cd3a9ac32e708b45476c3be5814a222d3aa8。运行 40515361238 的 backend、browser、android 三项全部成功；远端冻结后端回归 483 项 / 108.015 秒，零失败、错误及跳过。命令 gh pr checks 77、git pull --ff-only origin main 和 git ls-remote origin refs/heads/main 均退出码 0。部署后桌面与手机真实只读页面均加载比赛，无脚本异常或整页横向溢出。脱敏 CI 摘要见 evidence/collection-relay/github.json。
