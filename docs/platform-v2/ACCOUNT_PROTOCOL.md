# 用户账户协议与独立执行契约

日期：2026-10-26。当前阶段：协议证据已复核，隔离实现与 Fake HTTP 验证通过；真实联调尚未验证。没有连接真实账号或执行真实投注。全部源证据取自本仓库 Git 历史，不读取其它目录、旧远端配置或真实抓包秘密。

## 现有证据

| 能力 | 仓库内历史来源 | 已证实的契约 | 未证实的边界 |
| --- | --- | --- | --- |
| App 续期及场馆启动 | 旧 App 登录与会话适配、新共享续期模块 | 原生签名、登录专用短期代理、App token 与业务 requestId 分离 | 当前私人配置有效性与真实网络连通未核验 |
| 个人体育余额 | 旧账户适配 `VENUE_AMOUNT_PATH` | `/yewu12/api/user/amount`，可读明文 JSON 或业务封装，缺失不填零 | 多钱包转账及自动充值不在本次实现 |
| 个人订单 | 旧账户适配 `_fetch_venue_records`、正常化函数 | `/yewu13/v1/betOrder/client/getOrderListV4PB`；`orderStatus=0/1`、页号、大小、时间窗，支持压缩封装与按日分组 | 缺失订单不能证明未成交；总数不稳定不能证明扫描完整 |
| 当前盘口预检 | 旧账户适配 `prepare_bet` 及对应历史测试 | `queryLatestMarketInfo` 精确匹配比赛、盘口实例、玩法、选项、方向、原始赔率、状态与线 | 历史校验明确只接受滚球；赛前交易状态未核验，不猜测开放 |
| 原生限额 | 同上及旧离线抓包审计逻辑 | `queryMarketMaxMinBetMoney`，按选项读取 `minBet/orderMaxPay`，空可选玩法回显不当作冲突 | 当前真实限额及币种转换未核验 |
| 单关请求结构 | 旧投注协议构造、旧账户 `submit_bet` | `acceptOdds=2`、`seriesOrders` 单关结构、订单回执唯一编号及状态 0/1/2 | 未证实上游支持客户幂等键，不能把设备号当幂等键 |
| 查单结算 | 旧正常化与已结回执扫描 | 明确净利字段优先；返回本金及奖金时扣本金；保留订单与明细标识类型 | 手续费字段、独立通用赛事确认赛果接口缺证据 |

这里的来源是历史实现与其协议核验逻辑，不能冒充当前线上响应或本轮真实抓包。旧代码中的关闭 TLS 验证、浮点金额、403 自动登录重放及单一采集账号缓存不会迁移。

## 账户隔离与接口

`AccountScope(user_id, account_id, revision)` 使用两个明确 UUID 和正整数配置版本。`VenueSession(scope, base_url, request_id, origin)` 只接受对应用户账户自己的 HTTPS 会话。客户端构造时拒绝不同用户、账户或版本的会话。此模块没有读取环境账号、全局采集设置或本地凭据缓存的路径。允许的协议主机、签名和发现材料应由服务器管理；用户绑定输入不能任意指定内部 URL 或复用采集账号的 token、会话和设备身份。

用户账户通过 `AccountSessionManager(scope, private_config, RenewalHooks)` 调用共享 App 续期协议。私有配置只接受账户协议白名单，禁止旧全局 `leyu_api_token` 兼容别名。三个回调由业务 API 实现，并以用户及账号作为事务锁及鉴权作用域：

```text
claim(scope, upstream_identity_hash) -> attempt_id
persist(scope, expected_revision, updates) -> new_revision
finish(scope, attempt_id, status, safe_metadata) -> None
```

回调必须实现远端持久互斥、租约、未知/拒绝阻断、冷却、CAS、加密与审计。配置每次持久化后更新内存 scope 版本；不能把这些用户回调连接到采集账号的全局续期接口。所有业务采集及读取禁止环境代理，代理只存在于被授权登录事务。

`AccountProtocolClient(scope, session, transport=None, live_orders_enabled=False)` 支持异步上下文及关闭。默认关闭写入。仅提供有限路径，没有任意 URL/SQL/代理能力：

```text
balance() -> {available, currency, observed_at}
orders(state, page, size, begin, end) -> {items,total,has_more,malformed_records,...}
lookup(order_no, begin, end, max_pages) -> {found,receipt,scan_complete,absence_proves_rejection:false}
preflight(NativeSelection, Decimal stake, device_imei="") -> PreparedBet
submit_once(PreparedBet, SubmissionPermit) -> {state,order_no,provider_status,provider_code,execution_id}
```

`NativeSelection` 接受系统已绑定的原生比赛、盘口实例、玩法、选项、方向、项目、盘口分类、期间、十进制线及系统价格，禁止用 TG 原文赔率。预检并发读取三个无资金接口，精确校验后生成 15 秒有效的不可变准备结果。只使用原始当前盘口的请求线；市场显示格式变化用十进制比较，不能改成其它盘口。数据读取和下单网络异常不内联续期或重放。

业务 API 在账户事务锁下完成用户许可、专家范围、会员、单笔/每日/开放敞口、全局开关、推荐版本、价格新鲜度、配置版本校验和本金预留，持久化唯一 execution ID 后才颁发 `SubmissionPermit(scope, execution_id, prepared_digest)`。执行器拒绝跨用户/跨账户/跨版本/过期/篡改的准备结果和重复许可。内存防重只补充远端事务，不能替代数据库唯一约束。

写请求只发送一次。超时、传输错误、HTTP 拒绝、回执格式缺失或未知状态均进入 `SubmissionUnknown`，不释放敞口，不自动重发。明确业务拒单和订单状态 0 才为 `SubmissionRejected`；状态 1 为 accepted，2 为 pending。读取与写入共享的客户端也不会把写请求放入读取重试逻辑。

查单必须使用明确官方订单编号和时间窗。只找到明细编号时保留 `identifier_kind=detail`，不能冒充父订单号。请求受预算截断、重复页、缺字段及未知总数时均保留不完整标记；查单整体限时 60 秒。相同订单在相同结算状态中出现冲突回执时返回 ambiguous，不选择其中一条冒充权威结果。即使扫描结束，缺失也不证明未成交。提供方实际净收益、返回金额扣本金和手续费未知分别展示，禁止以缺失为零。

## 验证与未完成项

冻结后执行：20 项、0.333 秒、退出码 0，没有跳过。此前第一轮 19 项也通过，之后增加冲突回执和未知业务码终审情景再完整重跑。网络完全关闭，所有上游响应由 Fake HTTP 提供；测试仅模拟写请求，未发生真实请求或资金变化。

```sh
docker run --rm --cpus=0.5 --memory=384m --network none \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -v /root/ai_football:/w:ro -w /w ai-football-verifier:v2.0.0-dev \
  python -m unittest discover -s tests -p test_account_protocol.py -v
```

覆盖作用域与版本隔离、秘密不从环境回退、用户续期 CAS 回调、三个只读预检并发、原始盘口/赔率/方向/限额核对、非法或未知输入不填零、赛前交易不猜测、默认禁止写、准备结果篡改及过期、一次许可一次写、未知结果不重发、压缩订单、205 条订单分页、父子订单标识区分、截断或冲突扫描和真实净利计算依据。网络错误信息及上游原文不进入对外异常。

当前没有资金型验证证据。真实原生配置及独立用户设备身份、官方手续费与赛前交易规则仍需外部协议证据。模块导入需同时提供本仓库的共享采集协议包，不复制一份签名或续期实现。

## 账户任务与订单状态机

后续阶段已完成 API、远端任务、增量结构和独立执行器 Docker 入口，经过跨层隔离验证。绑定只保存本人提交的账号及口令，生成独立设备标识，并将身份与口令加密。只从采集配置复用协议主机、签名材料引用、动态发现、场馆及登录代理；严格白名单排除采集账号、token、requestId、Cookie 和设备身份。相同提供方身份不能同时绑定两个平台用户。

账户具有绑定版本及配置版本。绑定修改撤销本人自动投注许可、取消尚未发送订单、终止旧校验任务，且不释放已发送、未知或已接单敞口。在途资金请求不能撤回，因此其发送租约有效时重绑和解绑明确返回冲突；过期后按未知保留敞口，再允许修改绑定。

`POST /api/v1/accounts/leyu/verify` 或 `refresh` 将无资金任务排队；绑定也会排队。`GET /api/v1/accounts/leyu` 返回脱敏健康状态、余额及其观测时间、独立续期状态、任务与开发能力状态。解密失败不填空、余额未知不填零、开发适配器存在不代表实际交易已授权。绑定与校验有尝试限制。

执行器通过独立 Bearer 从 `/internal/executor` 认领任务。账户任务有租约、尝试编号、绑定版本，续期再领取独立账号锁；加密会话写入需要同时匹配任务、续期尝试与 CAS 配置版本。所有账户回调均有服务审计，秘密不进入日志。HTTP 库日志固定为 WARNING。新会话建立后必须实际读取有效余额，才能标健康。任务及续期结果具备重复回执处理，失败或退出不触发盲目重登。

```mermaid
stateDiagram-v2
    [*] --> queued: 本人绑定或校验
    queued --> running: 鉴权认领及租约
    running --> succeeded: 私有会话及有效余额
    running --> failed: 明确无登录的读取失败
    running --> unknown: 租约过期或登录结果未知
    running --> rejected: 上游明确拒绝
    unknown --> queued: 本人核对后重新绑定授权
    rejected --> queued: 本人核对后重新绑定授权
```

自动订单预留在账户事务锁下校验：用户身份、本人账户和许可版本、有效会员、允许专家、来源和作者信任关系、原文未删除及最新版本、滚球及玩法、系统价格精确匹配和真实源时间新鲜、单笔/上海自然日累计/开放敞口限额。重复用户与推荐只保留一个订单。采集或旧系统的许可不能继承到用户账户。

原生预检完成后，再在事务锁下复核全部条件及账户/配置/限额版本。准备摘要绑定用户、账户及版本；API 还验证实际请求内的原生标识、金额、赔率、盘口线、比分基线、币种、设备及单关结构，不能靠重算摘要改变交易内容。许可落库后才允许单发网络请求，资金路径不内联登录续期。

```mermaid
stateDiagram-v2
    [*] --> pending: 事务预留本金
    pending --> cancelled: 本人关闭或条件失效
    pending --> submitting: 复核通过并持久发送许可
    submitting --> accepted: 唯一官方订单号
    submitting --> rejected: 明确拒单
    submitting --> unknown: 超时或退出或未知回执
    accepted --> settled: 精确官方已结回执
    unknown --> accepted: 精确官方未结回执
    unknown --> settled: 精确官方已结回执
```

pending、submitting、accepted、unknown 保留本金预留；明确 rejected/cancelled 或精确官方 settled 才释放。执行器退出时，提交中的订单保持持久状态，发送租约过期只转 unknown，不重新排队。缺少官方订单号的未知请求不会猜测关联，也不会重发；它需要后续独立的人工证据核对能力，当前不自动释放。

查单每个账户任务最多两个已知官方订单，每个订单每种状态最多两页，完整扫描客户端另有总截止。任务预先记录允许核对的订单编号，回执超出范围拒绝；未找到只轮转检查顺序并保持敞口。相同提供方身份、用户与订单编号匹配后，官方已结回执才能结算该订单。退款或盈亏缺字段保持未知；手续费仍为未知。

用户订单入口 `GET /api/v1/accounts/orders` 仅返回本人记录，最大每页 100 条，按时间与编号键集分页。自动投注关闭与紧急停止始终可用，不受会员过期阻止；两者即时取消未发送预留，已在途请求继续通过官方回执核对。

## 跨层验证及部署契约

最终代码冻结后执行 38 项账户测试，10.376 秒、退出码 0、无跳过。其中 20 项协议测试，18 项使用每例 UUID 独立 PostgreSQL 库验证 API、任务和执行器。连接先校验专用 Docker 主机及专用建库角色；每例结束销毁本例数据库，不复用生产或其它任务库。此前 11、35、37 项阶段验证均通过，之后增加边界测试并完整重跑。

```sh
docker run --rm --cpus=0.5 --memory=512m \
  --network ai-football-v2-test-net \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e ACCOUNT_TEST_DATABASE_URL=postgresql://account_state_test:isolated-account-state-test-only@ai-football-v2-pgtest/postgres \
  -v /root/ai_football:/w:ro -w /w ai-football-verifier:v2.0.0-dev \
  python -m unittest discover -s tests -p 'test_account*.py' -v
```

跨层场景包含：真实共享续期实现通过 Fake App HTTP 完成本人密码登录、两次加密 CAS、场馆会话与余额校验；采集身份没有进入用户任务；并发幂等及限额；私有续期锁、重新绑定使旧写入失效；来源/会员/陈旧报价拒绝；本人订单隔离；重算摘要仍无法篡改金额及盘口；资金接口全部 Fake 的一次提交、未知不重排、发送中取消、租约过期敞口保留；紧急停止及在途身份保护；官方精确回执核对与幂等终态。

执行器使用独立构建入口，基础 Python 固定摘要，进程命令为 `python -m services.executor.worker`。只需要 API 服务令牌、`PLATFORM_EXECUTOR_API` 和被管理员批准的签名/发现材料只读引用；不接收数据库连接或模型密钥。`EXECUTOR_LIVE_ORDERS_ENABLED=false` 为执行器默认值，API 的实际交易开关也默认 false；管理员开关、本人许可及两侧运行开关全部有效才可能执行交易。隔离测试显式启用的分支始终使用 Fake HTTP，不构成真实资金授权。

生产 Docker 部署、有效本人配置验证、当前协议和网络接受性、失败恢复及正式发布仍须单独验收。本报告没有声称真实账户可登录、订单已成交或项目盈利。
