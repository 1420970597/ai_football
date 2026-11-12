# 未解决问题交接文档（2026-11-12）

分支 `fix/TASK-78-hermes-telegram-sync`。本文只记录**未解决**的问题与未完成的工作，供后续接手者直接按图索骥。已解决问题的过程记录见 `LAUNCH_READINESS_20261112.md` 与 `docs/platform-v2/` 下各专题文档。

阅读顺序建议：先看第一节（真实资金，最高优先），再看第二节（数据链路），然后按需查阅。

判断原则：本文只写可复核的事实与推断边界。凡是推断都标注「推断」并给出推翻它的检查方法，不把猜测写成结论。

---

## 一、真实资金相关（最高优先，未解决）

### 1.1 免费套餐携带 `automatic_betting` 权益

`v2_membership_plans` 中唯一套餐 `free`（「游客与免费用户」，启用，3650 天，价格 0）的权益为：

```json
{"expert_summary": true, "public_fixtures": true, "automatic_betting": true,
 "expert_full_history": true, "expert_live_signals": true}
```

注册即自动授予该套餐（`services/api/app/auth.py:266`）。`automatic_betting` 是
`orders.manual_policy()` 与 `orders.account_policy()` 的实际资金门槛，因此**任何完成乐鱼账户校验的注册用户都获得实盘资格**。用户 `aaa1111` 名下另有两条 active 的该套餐（系统授予至 2126-10-29、管理员授予至 2129-07-24）。

**为什么没修**：调整它改变真实资金准入面，属于只有所有者能决定的范围。

**可选修法**（需所有者批准其一）：

1. 从 `free` 套餐权益中删除 `automatic_betting`，另建付费套餐或在管理端手工授权该权益；
2. 保留 `free` 不动，在 `orders.account_policy()` 中增加「仅付费套餐可实盘」的显式判断（改的是代码而非数据）；
3. 维持现状，但在注册流程与会员页显式告知。

**验收方法**：新注册用户（无付费记录、无手工授权）调用 `PUT /api/v1/accounts/leyu/manual-betting` 应被拒绝并给出权益原因；已有手工授权用户不受影响。

### 1.2 实盘双开关为开启状态，已有真实资金成交

```
live_enabled(settings) = True          # LIVE_ORDERS_ENABLED=true
global_enabled(conn, settings) = True  # v2_admin_settings.auto_betting_enabled 解密为 true
```

`v2_bet_orders` 现状（2026-11-12 复测）：

| state | 笔数 | 本金合计 |
| --- | --- | --- |
| `settled` | 22 | 1055.00 元 |
| `cancelled` | 11 | 550.00 元（未提交） |

22 笔 `settled` 均带乐鱼真实订单号（如 `5382110238447533`），`provider_reported_net` 合计 **−202.5 元**。这些是真实资金成交，不是样本。

当前策略：`stake_per_order=10`、`max_per_order/daily_limit/open_exposure_limit=1000`、`auto_enabled=true`、`expert_selection_mode=profitable`、9 位允许专家。

**未解决的是判断本身**：这是产品决策（是否应在上线初期就开放真实自动投注），不是技术缺陷。技术侧开关、限额、幂等、查单链路均已实现并可用。若决定关闭，用管理端设置关 `auto_betting_enabled` 即可，不需要改代码或重启。

### 1.3 用户 `aaa1111` 的 `manual_stake = 0.00`

一键实盘买入按钮此前在该用户下**完全不渲染**，导致功能不可发现。候选镜像
`sha256:0c39e031be4c88c70a2af73635ca0e4746ef6d9e3c0ccb85b6708958df3f5778`
已改为「禁用 + 原因 + 指向设置面板」。因此**按钮可发现性已修，但该用户仍无法真正下单**，因为每单金额未设置。

**未解决部分**：这是用户数据，不应由开发侧代填。正确路径是用户在「设置 → 实盘」里设置金额。若要给他可用状态，需用户本人操作，或（经本人同意）由管理员代设一个有明确记录的值。

---

## 二、Telegram 采集：409 `version_content_conflict` 与 flood 退避（未解决）

### 2.1 现象

近 20 分钟采集器日志：`backoff request=GetMessagesRequest` **255 次**，
`POST /events` 返回 409 `reason=version_content_conflict` **52 次**，每次 409 都紧跟一条
`Telegram source unavailable category=HTTPStatusError`。409 的响应耗时 6–18 ms，说明是 API 侧内容比较的主动拒绝，不是网络或超时。

被拒绝的请求不是全部都属异常——同窗口内 `operation=events status=200` 的请求同时存在（**1033 次**），所以链路整体在工作。

### 2.2 已证实的事实（可复核）

1. **库内一侧自洽**，用本轮四条独立检查确认：
   - `content->>'version_key'` 与 `version_key` 列不一致的行：**0**
   - `(source_id, platform_message_id, version_key)` 三元组重复：**0**
   - `content` 的 JSON 键集形状：全部 2685 行为**同一个** 13 键形状（去重后 1 种）
   - 按采集器同一算法（排除 `owner`/`sender_nickname`/`media_objects`/`version_key`）对**全部 2685 行重算哈希**：**2685 匹配 / 0 不符**
2. **时间字段格式统一**：`source_version_at` 与 `posted_at` 全部为 `+00:00` 后缀，无 `Z` 后缀行（各 2688 行）。排除了「同一时刻两种字符串序列化」这一最常见的冲突来源。
3. **数据库没有中断**：近 20 分钟仍入库事件、`v2_current_quotes` 最新报价滞后 <1 秒。
4. **相册展开不是主因**：近 20 分钟 `album_expansions` 仅 4 次，与 52 次 409 不在同一量级。
5. **历史事件类型只有 `message` 与 `edit`**：零 `delete` 事件，零 `delete:` 前缀 id，所以删除路径不参与。
6. **9 条 `pending` 媒体事件** 均为 `is_current=false` 的历史版本（最早 2026-11-09）；`/internal/telegram/pending` 只取 `is_current`，因此它们既不会被重试，也不阻塞当前版本。

### 2.3 定性结论与剩余推断

服务端判定规则（两处，必须同时满足才 409）：

- 快速路径 `services/api/app/routes/telegram.py:384`：
  `v.id IS NOT NULL AND (v.content-'sender_nickname') IS DISTINCT FROM (i.content-'sender_nickname')`
- 慢速路径 `services/api/app/routes/telegram.py:892`：同一条规则用 Python 字典比较重写

其中 `v` 是按 `version_key` 命中的**已存版本**，`i` 是**本次提交体**。既然库内 2685 行全部可由自身内容重算出正确的 `version_key`（检查 1 的第四项），那么命中同一 `version_key` 的已存行内容必然等于当初计算该键时使用的指纹。**因此 409 的唯一可能来源是：采集器提交的 `content`（即提交体除 `owner`/`media_objects`/`media_state`/`last_error`/`reconciliation` 之外的字段）与该 `version_key` 所对应的原始指纹不一致。**

采集器只有一处计算 `version_key`（`services/collector/telegram.py:1020`），指纹**包含** `media_state` 与 `last_error`，但服务端 `content` **不包含**这两个字段。这是一个真实存在的字段集不对称：

- 若某条消息第一次以 `media_state="pending"` 提交并入库，之后采集器以 `media_state="ready"` 重新暂存同一条消息 → 指纹变化 → 产生**新的** `version_key` → 命中不到旧行 → 不会 409（这条路径是安全的）。
- 但如果同一 `version_key` 的两次提交之间，**指纹之外**、**content 之内**的某个字段发生了变化（`reply_context` 从 `{}` 变为 `{"unavailable": true}`，或 `raw_text`/`member_message_ids` 因相册成员枚举顺序/边界不同而不同），就会命中旧行并触发 409。

**这是推断，不是结论。** 关键不确定性：指纹与 content 都基于同一个 `payload` 字典构造，理论上应同步变化；出现不一致只能来自「同一 `version_key` 的两次提交用到不同的 `payload`」，而代码里没有缓存或复用 `payload` 的路径（`version_key` 只在 `_stage_group` 内计算一次，`telegram.py:901/1271` 的两处引用分别是回执与删除路径）。

**推翻/证实它的检查方法**（尚未执行，需要采集器把提交体落盘或加日志）：

```python
# 在 services/collector/telegram_backend.py 的 409 分支加一次性诊断日志：
# 对比 item['content'] 与响应体回传的已存 content 的差异字段名（不回显正文）
```

或更轻的替代：在 `telegram_backend.py:20-26` 的 reason 映射附近，捕获 409 时把
`sha256` 前 12 位与 `platform_message_id`、`source_version_at`、`member_message_ids` 记入一行
`log.warning(..., category=conflict_diag ...)`，跑 10 分钟后比对库内同键行。

### 2.4 409 的两种含义，必须区分

- **重复提交的正常幂等拒绝**：内容确实相同，只是 `version_key` 命中而内容比较因浮点/布尔/null 归一化差异误判。此时**没有数据丢失**，只是日志噪音 + 来源被误标 unavailable。
- **采集器重建了不一致的 payload**：此时该版本的消息内容**未按新内容落库**，属于真实数据丢失。

当前证据（库内 2685 行全自洽、报价新鲜度正常、事件持续入库）**倾向于**前者，但**未证实**。在证实之前，不能宣称采集链路无数据丢失。

### 2.5 已知的设计缺陷：409 被映射为 `source unavailable`

`services/collector/telegram.py:530` 把任何非 flood 异常统一映射为
`log.warning("Telegram source unavailable category=%s", ...)`。409 是**内容冲突**（确定性拒绝，重试无意义），把它标成「来源不可用」会：

1. 误导排障方向（看起来像账号/网络问题）；
2. 若上层据此做来源级退避或告警，会放大噪音。

**建议修法**：在 `telegram_backend.py` 中把 409 映射为独立异常类型（如
`VersionConflictError`），在 `sync_once()` 里单独处理：记录 `platform_message_id` 与差异字段名，**不**标记来源不可用，且**不**触发来源级退避。这与 §2.4 的诊断改造天然合并。

### 2.6 Flood 退避：现状正确，但检查点与已入库最大 id 落差巨大

`backoff request=GetMessagesRequest` 20 分钟 255 次是 Telegram 侧限流，采集器已有退避处理（`retry_at`），这一点是**正常防护**，不是缺陷。

但检查点存在显著落差，需要后续确认是否为退避的诱因：

| 来源 | checkpoint | 已入库最大 id | 差值 |
| --- | --- | --- | --- |
| 足球 五大联赛交流群 红单推荐【每日滚球】 | 478203 | 1640229 | 1,162,026 |
| 足球推文·初盘滚球·福利包赔专区 | 132941 | 170203 | 37,262 |
| 五大联赛足球篮球滚球电竞初盘球料红单猛料群 | 82922 | 120548 | 37,626 |
| 【全网大神解锁单】… | 197238 | 213352 | 16,114 |

原因已定位：`_sync_source()` 用 `min_id=original` 从头追赶，每轮受
`SYNC_HISTORY_MAX_MESSAGES` 与 `SYNC_HISTORY_SLICE_SECONDS` 切片限制，`catching_up` 会持续很久。**这在设计上是「有预算的全量初始化」**（AGENTS.md 第八节第 4 条要求），本身不算缺陷；但当历史消息量为百万级时，追赶窗口会非常长，期间每轮都在向 Telegram 要历史，与实时消息共享限流预算。

**未解决**：是否为百万级历史设置一次性「跳过到底部」的初始化策略（记录跳过的 id 区间并标注「历史未回补」），还是继续按切片追赶。前者快但会永久丢掉该区间内的历史推荐；后者慢但完整。需要产品判断。

**另**：`Leyu poll state=partial` 每 8–12 秒出现一次（`last_success_at` 持续前进），与报价新鲜度不受影响一致，属部分子步骤失败而非整轮失败，本轮未细究。

---

## 三、媒体与审核链路（未解决）

### 3.1 `unsupported` 媒体门

`services/api/app/routes/telegram.py:338` 要求 `media_state == "ready"` 才创建审核任务。媒体被判为 `unsupported` 的消息**永不进入审核**。近 72 小时统计：

| media_state | 条数 |
| --- | --- |
| `ready` | 2264 |
| `unsupported` | 414 |
| `pending` | 9 |

当前 414 条中绝大多数无正文（纯图/贴图），不误伤真实推荐。**未解决的是长期风险**：若某条真实推荐以「图片 + 说明文字过长/像素超限」形式出现并被判 `unsupported`，它会被静默丢弃且无告警。

**建议**：对 `unsupported` 且有非空 `raw_text` 的事件单独计数并告警，而不是依赖人工抽样。

### 3.2 约 2045 条历史 `unresolved` 审核任务

`v2_analysis_jobs` 中 `state='unresolved'` **2045 条**，最早 `2026-10-29`。这些是历史抽取失败的积压。用户已明确「抽取准确性不构成本次上线门槛」，因此**不影响上线判定**，但它们是「专家发言识别仍存在 bug」这一用户诉求的直接证据面。

**未解决**：是否需要批量重放。重放的前提是原始消息仍在保留期内（TG 原文 72 小时），而 2026-10-29 的消息原文早已过期，所以**这批任务大概率无法重放**，只能作为统计口径说明（例如在专家页标注「历史未匹配样本」）。需要先确认 `v2_expert_messages` 的 `raw_text` 是否仍在（原文保留策略与审核轨迹保留策略不同）。

### 3.3 审核任务创建依赖 `media_state == "ready"`

见 3.1。纯文本消息的 `media_state` 也是 `"ready"`（`telegram.py:1022`：无媒体时直接 `"ready"`），所以此门不影响纯文本，只影响媒体消息。

---

## 四、基础设施与运维（未解决）

### 4.1 磁盘 85%，`.runtime` 占 28 GB

```
/            251G  205G   37G  85%
/root/ai_football/.runtime   28G
```

已知大项：`perf-backup-20261028` 6.4 GB、五个 `ai-football-e*-restore-data` 卷约 16.4 GB、本轮上线转储 `launch-20261112/football_v2-post037.dump` 约 102 MB。

**未解决**：五个 `ai-football-e*-restore-data` 卷的留存价值未评估。它们对应已完成的 e 系列实验，**按 AGENTS.md 第二节第 5 条，在研究并入库清理方案前不得删除**。需要先确认：这些卷是否含未迁移数据、是否已被 `football_v2` 覆盖、恢复演练是否依赖它们。

### 4.2 遗留容器 `ai-football-v2-pgverify`

2026-10-26 启动，`--restart=no`，`Up 2 weeks`，挂载卷 `a31185d3…`。不是本项目业务容器，属遗留验证用途。**未删除**（按约束不得停掉非本项目服务；虽名称带本前缀，但功能归属未确认）。

同时存在 8 个 `ai-football-e*-*` 实验容器（`e1263-anchor-web`、`e3056/e3083/e3104/e3127/e3141-full-postgres`、`e3139-api/hermes/mcp`）与 `buildx_buildkit_ai-football-e1310`，均为实验残留在运行。

**未解决**：这批容器的归属确认与清理。清理前需逐个确认是否仍在提供流量或数据。

### 4.3 无当前 Android 候选包

`android/build/artifacts/` 最新为 **2026-10-28** 的 dev 包（约 40 KB），与当前提交不对应。**未解决**：需要从当前提交重新构建并签名，然后验证安装、服务配对与离线状态。

### 4.4 镜像与迁移账本数量不一致

`raw-retention` 与 `local-mcp-api` 镜像内含 **35** 条迁移，而库内 `v2_schema_migrations` 已应用 **36** 条（含 037）。说明这两个镜像的分支晚于 037 迁移的引入点。**未解决**：下次统一构建时需确认它们是否需要感知 037；当前不影响运行，因为迁移由 api 启动时执行。

### 4.5 历史密钥暴露

某次历史会话中 `S3_SECRET_KEY` 曾被回显到会话记录。**建议轮换该密钥**（本文不复述其值）。轮换步骤：在 `minio-20261111/local-storage.env` 生成新密钥 → 更新 MinIO 用户 → 更新 api/collector 的环境 → 重启这两个服务 → 验证一次媒体上传与下载。

---

## 五、前端与体验（未解决，已被用户指定为下一阶段）

用户最新指令：「重构新版视觉前端，由你设计，委托子代理使用opus5.5执行」。

### 5.1 现状基线

| 文件 | 行数 | 字节 |
| --- | --- | --- |
| `web/app/app.css` | 2322 | 42875 |
| `web/app/app.js` | 2056 | 103900 |
| `web/app/index.html` | 986 | 46568 |
| `web/app/admin.js` | 925 | — |
| `web/app/ui.js` | 300 | — |
| `web/app/identity.js` | 203 | — |

现有设计变量（`app.css:1-20`）：
```css
--purple:#0868c5  --purple-dark:#07539c  --orange:#ef7d26  --orange-text:#aa4809
--red:#bd382f     --green:#14663f      --ink:#283443     --muted:#5c6978
--line:#e0e7ee    --paper:#fff         --background:#f3f6f9  --gold:#ad7b32
font: 14px/1.5 Arial, "Microsoft YaHei", "PingFang SC", sans-serif;
```

现有视图与关键 id：`view-scores`、`view-experts`、`view-messages`、`view-membership`、`view-account`；`manual-bet-panel`/`manual-bet-form`/`manual-bet-status`、`matches-list`/`matches-state`/`load-more`、`plans-list`、`experts-list`/`expert-ranking`/`expert-signals`、`messages-list`、`identity-panel`、`account-content`。

### 5.2 必须保住的验收底线

`.runtime/refactor-wt/web-ux/tests/browser/manual-bet.mjs`（166 行，Playwright，全路由不打真实账户）是既有契约，重构**不得**破坏：

- 每种阻断原因都要禁用按钮并用 `aria-describedby` 关联解释（「尚未保存每单下注金额」「实盘执行当前未开放」「本人乐鱼账户尚未通过校验」「当前会员没有实盘交易权益」「紧急停止」）；
- 未设金额时按钮仍须可被发现；
- 买入控件高度 ≥ 44 px；
- `#confirm-message` 须含「利物浦 对 曼城」「林老师 TG」「亚洲让球」「1.96」「2.00 元」，且「全场」恰好出现一次；
- `#confirm-title` 为「确认实盘买入」；
- 结果面板须比 toast（5.2 s）更久，409 拒绝须显示服务端原话且按钮保持可用。

### 5.3 未解决的性能项

- 静态资源无指纹：`/assets/app.js`、`/assets/app.css` 固定路径，无法长缓存。需加内容哈希并配 `immutable`。
- 无 ETag/304：每次导航都重传 100 KB JS + 42 KB CSS。
- 首页轮询无自适应：固定间隔，页面不可见时仍在拉取。
- 图片资源未压缩/未响应式。

---

## 六、待决事项汇总（按优先级）

| # | 事项 | 类型 | 阻塞点 | 建议动作 |
| --- | --- | --- | --- | --- |
| 1 | `free` 套餐含 `automatic_betting` | 真实资金 | 需所有者决策 | 见 §1.1 三个选项 |
| 2 | 实盘双开关开启 + 22 笔真实成交 | 真实资金 | 需所有者决策 | 见 §1.2 |
| 3 | `aaa1111` 的 `manual_stake=0` | 数据 | 需用户本人 | 引导用户设置 |
| 4 | 409 `version_content_conflict` 定性 | 数据链路 | 需加诊断日志 | 见 §2.3 检查方法 |
| 5 | 409 被映射为 source unavailable | 设计缺陷 | 无 | 见 §2.5 |
| 6 | 百万级历史追赶策略 | 产品 | 需产品判断 | 见 §2.6 |
| 7 | `unsupported` 媒体静默丢弃 | 数据链路 | 无 | 加告警计数 |
| 8 | 2045 条历史 unresolved | 数据 | 原文可能已过期 | 先查 `raw_text` 是否仍在 |
| 9 | 磁盘 85% / 16.4 GB 实验卷 | 运维 | 需评估 | 先做留存价值研究 |
| 10 | 遗留容器与卷清理 | 运维 | 需归属确认 | 逐个确认 |
| 11 | 无当前 Android 包 | 客户端 | 需构建 | 从当前提交重建 |
| 12 | 镜像迁移数 35 vs 36 | 构建 | 无 | 下次统一构建修正 |
| 13 | `S3_SECRET_KEY` 轮换 | 安全 | 无 | 见 §4.5 |
| 14 | 前端视觉重构 | 体验 | 已排期 | 见第五节 |
| 15 | 前端性能（指纹/ETag/自适应轮询） | 性能 | 无 | 见 §5.3 |

---

## 七、工作树状态（交接时必须知道）

以下内容为**他人（Codex 会话）未提交的修改**，本轮未触碰、未提交、未还原：

```
 M services/api/app/account_jobs.py
 M services/executor/account_protocol.py
 M services/executor/worker.py
 M tests/test_account_protocol.py
```

其内容是小额订单批量对账改进：`lookup_many()` 一次扫描账户历史、按订单号分别判定歧义、`reconcile` 单次调用、订单历史读取失败时**保留敞口**并记录
`order_reconciliation_partial`。该改动自带测试
`test_lookup_many_reads_pages_once_and_keeps_conflicts_order_scoped`，26/26 通过（Docker 内 unittest）。

未跟踪目录/文件：

```
?? .omc/                                          # OMC 工具状态，不入库
?? .pi/                                           # Pi 工具状态，不入库
?? docs/platform-v2/REVIEW_RESULT_SPORT_STORAGE_20261112.md
?? reports/feijipan-share-20261031/               # 4.1 GB / 5158 文件，禁止入库
```

`reports/feijipan-share-20261031/` 是 2026-10-31 对 34 个复制投注/投注工具样本（1.55 GB）的静态 REA 研究，结论为「静态未定性、供应链高风险、禁止生产运行」。**只有 `REPORT.md` 与 `evidence/*.json`（约 4.7 MB）具备入库价值**，其余 4.1 GB 须走文件投放或删除。**未解决**：需要决定该目录的最终去向。

---

## 八、复现命令

```sh
# 容器与健康
docker ps --filter 'name=ai-football-v2-' --format '{{.Names}}\t{{.Status}}'

# 数据新鲜度
docker exec ai-football-v2-local-storage-local-postgres-1 psql -U football_admin -d football_v2 -c \
  "SELECT now()-max(received_at) AS lag FROM v2_current_quotes;"

# 实盘成交
docker exec ai-football-v2-local-storage-local-postgres-1 psql -U football_admin -d football_v2 -c \
  "SELECT state,count(*),sum(stake) FROM v2_bet_orders GROUP BY 1 ORDER BY 2 DESC;"

# 409 与退避（近 20 分钟）
docker logs --since 20m ai-football-v2-collector-1 2>&1 | grep -c 'reason=version_content_conflict'
docker logs --since 20m ai-football-v2-collector-1 2>&1 | grep -c 'backoff request='

# 库内自洽（应为 0 / 0）
docker exec ai-football-v2-local-storage-local-postgres-1 psql -U football_admin -d football_v2 -tAc "
SELECT count(*) FROM v2_telegram_events WHERE content->>'version_key' IS DISTINCT FROM version_key;"

# 检查点落差
docker exec ai-football-v2-local-storage-local-postgres-1 psql -U football_admin -d football_v2 -c "
SELECT s.display_name, cp.last_message_id AS ckpt,
       (SELECT max(e.platform_message_id::bigint) FROM v2_telegram_events e
         WHERE e.source_id=s.id AND e.platform_message_id ~ '^[0-9]+\$') AS max_ingested
FROM v2_expert_sources s LEFT JOIN v2_source_checkpoints cp ON cp.source_id=s.id
WHERE s.enabled ORDER BY 2 DESC;"

# 未提交改动（交接核对）
git status --short
```
