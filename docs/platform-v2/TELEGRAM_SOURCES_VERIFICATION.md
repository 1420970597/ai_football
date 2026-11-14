# Telegram 群组、管理员与手工信任配置验证

更新时间：2026-10-26 12:52 UTC。本阶段状态：服务端隔离验证通过，源码已冻结。网页由另一代理接入并单独验收；真实 Telegram 账号、群组权限与头像读取没有联调。

## 复核范围与实际缺口

复核既有采集目录、管理 API、来源信任映射以及网页调用者，没有重新建立平行目录服务或数据表。修改限定于采集目录逻辑、Telegram 管理目录代理、既有来源及信任接口、相关测试；没有修改其他订单、账户或审核业务。

发现并修复两项实际契约问题：

1. 采集器原本返回目录状态 `ready`，网页管理员列表仅接受 `ok`、`available` 或无状态，导致成功响应仍不能展示。公开 API 现在将目录成功统一为 `available`；私有采集控制接口维持 `ready`，其他失败状态及原因原样保留。
2. 固定依赖 Telethon 1.42.0 的普通小群参与者迭代没有应用超级群管理员过滤器，原逻辑可能把所有普通成员标为管理员。现使用普通群完整成员角色，只接受 `ChatParticipantAdmin` 与 `ChatParticipantCreator`；超级群使用 `ChannelParticipantsAdmins` 服务端过滤及真实 RPC 分页。测试使用真实 TL 实体与角色类型，不以“假迭代器只返回管理员”掩盖错误。

## 设计与实现

- 已加入群组/频道目录支持名称、稳定 peer 编号、用户名及 `@用户名` 搜索，不限于首屏五十条。结果按稳定 peer 编号排序，避免对话因新消息改变最近顺序而导致 offset 页面重复或遗漏；私人聊天、已退出、禁访及已迁移的旧实体不会冒充可用群组。
- 来源可用负数稳定编号或 `@公开用户名` 进行实体解析；StringSession 没有实体访问哈希时，从全部已加入对话补找。数字正号、个人编号、群名与任意 URL 不会被猜成目标群。用户名解析响应给出规范负数编号，持久监控来源必须使用该稳定编号。
- 作者目录也支持 `q`：按昵称、稳定 sender ID、用户名及 `@用户名` 检索。超级群有搜索条件时读到管理员末页后再筛选并分页，不只搜索首五十人；使用服务端总数处理被缩短的页，重复页、缺失角色/用户及中途失败均拒绝返回部分成功。控制接口既有整体截止时间保持有效，超时不是“没有管理员”。
- 管理员列表显示稳定 ID、昵称、用户名、拥有者/管理员角色，并明确匿名管理员可能不在枚举结果中。`has_avatar` 与 `avatar_state` 只报告已知元数据，未获取头像字节，不能据此勾选头像展示完成。
- 手工信任继续使用已有来源与作者映射接口。群组 ID、作者 ID 做规范化，空白名称、昵称身份键及零编号被拒绝。相同编号更新昵称而保持同一作者；同名不同编号不会合并。来源编号创建后不能原地换成另一个群，以保护历史原文与既有信任映射。
- 信任新增响应明确 `trust_basis=manual`、`telegram_admin_verified=false`；列表标为 `configured_mapping`，不虚构旧导入记录的核验来源。审计持久记录来源编号、sender 编号、核验说明和身份类别，移除信任也带来源编号。同一作者在其他授权来源中的映射不受影响。
- 手工允许正数用户账户和负数群组/频道 posting peer。负数不会推断成背后的某一名匿名管理员，按发言 peer 聚合；响应、列表与审计使用 `sender_identity_kind=user_account|chat_sender|unresolved`、`individual_author_resolved=false`。实际消息保留真实 sender 编号与其 Telegram 昵称/频道标题，不把所在群名自动作为某个人昵称。
- 来源和信任写入在事务锁后复核当前管理会话与近期验证，保留已有 CSRF、审计及幂等映射。没有新增迁移或改动配置版本比较与交换。

## 对外契约

| 接口 | 查询/请求 | 成功响应重点 |
| --- | --- | --- |
| GET `/api/v1/admin/telegram/dialogs` | `q`（最多160字符）、`cursor` | state=available、items、next_cursor、q、joined_only=true；items 含 chat_id/title/username |
| GET `/api/v1/admin/telegram/admins` | `chat_id`（负数规范编号或@用户名）、`q`、`cursor` | state=available、items、next_cursor、q、规范 chat_id/title、manual_id_allowed、anonymous_admins_may_be_unlisted |
| POST `/api/v1/admin/sources` | chat_id、display_name、enabled、allow_media | 稳定规范来源；重复为409，无效编号或空白名称为422 |
| PUT `/api/v1/admin/sources/{source_id}` | 原 chat_id 与新状态/名称 | 可停用或改展示；换群为409，需要另建来源 |
| POST `/api/v1/admin/sources/{source_id}/experts` | sender_id、nickname、可选 verification_note | 来源独立的人工信任标记、作者稳定身份类别；不声称已由Telegram验证管理员身份 |
| GET/DELETE `/api/v1/admin/sources/{source_id}/experts[/{expert_id}]` | 来源与稳定专家编号 | 查询配置关系/只停用当前来源关系 |

修改搜索内容或切换来源必须清空旧 cursor；空查询恢复完整目录。每页最多五十条，无下一页时返回 null。新增字段对旧调用兼容。

错误目录仍有 `items=[]`，但不能当空成功：未授权为 `unauthorized`，限流为 `flood_wait` 并带 retry_after，其他访问失败为 `unavailable` 并带 reason。原因包括：

| reason | 含义与处理 |
| --- | --- |
| account_has_not_joined | 当前账号未加入或已退出；先修复账号访问 |
| account_has_not_joined_or_entity_is_unavailable | 缓存与已加入目录均无法解析；没有证据进一步区分未加入或实体不可用 |
| source_not_accessible | 来源被禁访或不可访问；手工作者编号不能修复来源权限 |
| source_not_group_or_channel | 所解析实体是个人等错误类型，不作为监控来源 |
| source_migrated | 普通群已迁移，响应含 resolved_chat_id；由管理员明确配置新来源 |
| member_enumeration_restricted | 账号可解析来源但无权枚举成员；允许手工添加已核验发言编号 |
| invalid_source_identifier | 非法或不支持的来源标识 |
| invalid_pagination_cursor / invalid_search_query | 非法分页或搜索；公开API在调用控制服务前返回422 |
| network_unavailable / upstream_unavailable | 网络/其他上游故障，不能解释为无管理员 |
| incomplete_admin_page / directory_pagination_stalled | 成员证据缺失或分页不前进，停止而不返回不完整搜索成功 |

## 冻结测试与真实退出码

应用及测试全部在 Docker 内执行。项目只读挂载，先复制到容器内部冻结副本；没有在运行中修改被测副本。

```sh
docker run --rm --name ai-football-v2-telegram-directory-test \
  --cpus=.5 --memory=256m --network=none -e PYTHONDONTWRITEBYTECODE=1 \
  -v /root/ai_football:/source:ro --entrypoint sh ai-football-collector:v2.0.0-rc1 -c \
  'mkdir -p /tmp/tg-frozen/tests && cp -a /source/services/collector /tmp/tg-frozen/collector && cp /source/tests/test_telegram_collector.py /source/tests/test_telegram_directory.py /tmp/tg-frozen/tests/ && cd /tmp/tg-frozen && python -m unittest discover -s tests -p "test_telegram*.py" -v'

docker run --rm --name ai-football-v2-telegram-source-test \
  --cpus=.5 --memory=384m --network ai-football-v2-test-net \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e TELEGRAM_TEST_DATABASE_URL=postgresql://postgres@ai-football-v2-pgverify:5432/postgres \
  -v /root/ai_football:/source:ro --entrypoint sh ai-football-api:v2.0.0-rc1 -c \
  'mkdir -p /tmp/tg-frozen/tests && cp -a /source/services/api/app /tmp/tg-frozen/app && cp -a /source/services/api/migrations /tmp/tg-frozen/migrations && cp /source/tests/test_telegram_api.py /source/tests/test_telegram_sources.py /tmp/tg-frozen/tests/ && cd /tmp/tg-frozen && python -m unittest discover -s tests -p "test_telegram*.py" -v'
```

最终采集 **35/35 通过、零跳过，0.811秒，退出码0**；最终API **25/25 通过、零跳过，19.629秒，退出码0**。采集容器禁用网络；API每项创建和销毁独立临时 PostgreSQL 数据库，控制服务使用显式 MockTransport，没有真实 Telegram 请求。`git diff --check` 本阶段相关路径退出码0。

中间记录如实保留：首轮采集31项有一项新测试直接调用内部实体解析前没有连接假客户端，退出码1、0.781秒；修正测试夹具建立假连接后同31项退出码0、0.726秒。首轮API24项退出码0、18.814秒。随后新增作者q搜索及负数发言身份案例，形成上述最终35/25冻结；没有放宽生产实体或权限检查来掩盖夹具失败。

核心样本包括：121个群组跨页不重复、首106群后目标仍可搜索、两种历史问题群编号形态的缓存补找、普通成员不冒充管理员、123名管理员分页、405名管理员搜索及服务器100条短页、分页失败/卡住、管理员访问受限与FloodWait、规范ID去重、来源不可换绑、同名不同身份、同作者跨来源独立、人工信任审计失败完整回滚、负数频道身份消息。

## 冻结内容摘要

基准父提交为 `011433febea15fe45d3b417f81e4706102205fcb`，本阶段源代码尚由主代理审阅提交，不能声称以下未提交增量已被远端CI验证。

| 内容 | SHA-256 |
| --- | --- |
| 既有来源与信任管理局部 | 7c420882d3358266f09025c22d004bb07bbbf06f86305b4b3282880e7b17623f |
| Telegram API目录代理 | 8a0561cd120450176b7286692273f303102d4960ea8df3f95a1ea926379676ef |
| Telegram采集与目录 | c54f5197a37bffb91f1d0ea5d07684480be3389210d420fce22d4bb6eb078d33 |
| 既有采集验收补充 | cf5f837088b327358c8df4a6a773bc4f2c258c40603006ef9654bf3519f02464 |
| 目录行为验收 | df000b9228ebd7c9ccfa056a33571ed196023d58f51ae310c8f2187364680804 |
| 来源/信任API验收 | 82e9b2941a4cca8e607d9d9d3809f6a7e1d37ee6ce8af2227f4261c613dfb7a9 |

## 可更新任务状态与未完成项

P21-01 的群组读取与搜索服务端、P21-04 手工信任接口、P21-05 来源独立映射达到隔离验证通过；群组和作者q已交网页代理独立整合。P21-02 仅验证历史编号格式、缓存补找和权限分类，不能据此勾选真实问题群组权限联调。P21-03 的稳定身份/昵称/用户名/角色有假RPC证据，真实头像授权读取没有实现或验收。P21-06 的网页紧凑布局、双搜索、清空和游标重置须以网页代理实测为准。

未读取项目外受保护配置，未访问真实账号、真实群组或远端目录，未运行真钱。没有改动主任务清单或主进度文件，也没有自行提交、推送或发布。

## 2026-10-27 自动同步与普通成员扩展

既有管理员接口与角色筛选保持独立。新增 GET /api/v1/admin/telegram/members，界面默认“所有成员”，支持昵称、用户名和稳定编号，普通成员选用仍须明确保存信任映射。每页50，实际继续翻页，不把首屏当全体；精确用户名先核验当前来源的成员身份。无权限和隐藏成员返回可操作原因，不绕过 Telegram 限制。

采集租约内首次和每五分钟同步管理员全目录；可信集合变化提前刷新。完整成功快照才替换旧目录，昵称按稳定身份独立更新并保留别名，普通成员成为可信作者后同样刷新。目录及历史在远端 PostgreSQL，消息图片仍在远端S3。

实际已授权会话四个来源：两个目录 ready（7和11名），两个 member_enumeration_restricted，四个来源均有有效采集租约，远端昵称历史4条。当前真实来源没有返回普通成员；普通成员的分页、搜索、选用和消息信任使用协议夹具与真实浏览器验证，不声称真实群公开隐藏成员。

证据：evidence/hermes-telegram/directory.json。最终完整回归由 E100 统一追加，首轮 495 项 /188.124秒通过；本阶段首次两项SQL时间类型冲突及浏览器预期失败已保留并修复。
