# 身份恢复、近期验证与本人数据生命周期

更新时间：2026-10-26 11:47 UTC。状态：服务端隔离验证通过；前端真实 API 与 HTTPS WebView 联调仍由客户端验收阶段核对。本记录不代表存在邮件、短信或支付通道。

## 设计及边界

公开恢复状态明确 `delivery_configured=false`、`self_service_available=false`。请求发送恢复消息返回 503，并说明系统没有发送；不会按不存在的外部渠道返回“已发送”。管理员核验普通用户身份、再次认证有效后，填写原因及幂等编号，人工签发十分钟一次性恢复码。数据库只存摘要，响应首次显示原码；幂等重放只返回编号与期限，不能从数据库恢复原码。重新签发撤销旧码，消费采用用户行锁及条件更新，成功更换慢哈希密码、撤销该用户全部会话并审计。

管理员可生成十个一次性备用登录码，保留摘要并在使用后标记消费；新批次撤销旧批次。备用登录仍要求管理员密码，成功撤销其他管理会话。码只显示一次，不写审计原文，不自动发送。备用码是已配置 TOTP 丢失后的预防性恢复方式；没有事先保存备用码且 TOTP 同时丢失时，不能通过公开接口跳过二次验证。TOTP 更换手机的重新登记界面仍需后续完善，当前不声称已实现。

2026-10-29按最新用户要求，普通用户及管理员登录会话统一180天，Cookie Max-Age为15552000秒，Expires与服务端expires_at一致；原管理员一小时及空闲十五分钟限制取消。已有会话仍以其保存的到期时间为准，重新登录或普通用户合法刷新取得新期限，不复活过期或撤销凭据。敏感写入要求五分钟内再次核验当前凭据；TOTP按账号当前配置执行。普通用户绑定乐鱼和开启自动投注、导出本人数据，也要求近期密码验证。刚登录算已验证；刷新普通会话保留原验证时点，不能悄悄延长敏感操作窗口。退出与停投仍可执行。过期重验返回 `403 detail.code=reauthentication_required`，带明确会话上下文，前端应保留原表单、独立重验并要求用户重新提交，不能自动重放资金操作。

密码恢复及普通改密同时撤销原自动投注许可，取消未发送订单，保留已发送和未知回执。绑定及启用许可在事务中按用户、账户顺序加锁，再检查会话摘要仍存在且近期验证有效，防止密码恢复前通过入口校验的旧请求等待后重新开启许可；关闭和紧急停止始终可执行。

本人导出按资料、会员和订单分区，订单及会员采用有上限的键集分页，永远绑定当前用户编号。白名单输出排除密码、令牌、第三方凭据及内部安全原始载荷；导出记录审计，不在本机创建持久导出文件。

注销必须再次输入当前密码及明确确认语句，先关闭自动投注、撤销许可并取消未发送订单。未知、发送中、已接单未结算订单或运行中的账户任务使注销进入 `pending_accounting`，保留核对所需连接与原始回执，仍能读取本人账目、导出和紧急停止；禁止重新绑定、开启投注及普通资料写入。用户可以取消注销，但自动投注保持关闭；核对结束后再次确认注销，才能清空第三方凭据、撤销会话、取消权益、匿名化邮箱和昵称。保留稳定用户编号及订单、成交、结算、审计证据，未执行整行级联删除。

## 接口契约

路径均以 `/api/v1` 开始。管理写入须管理 Cookie/CSRF，普通写入须用户 Cookie/CSRF；公开恢复消费依赖高熵一次性码及独立限流。

| 接口 | 核心输入/输出 |
| --- | --- |
| `POST /auth/reauthenticate` | 输入 password；返回 verified、session_kind、expires_at |
| `POST /admin/reauthenticate` | 输入 password、totp_code；验证码防重放 |
| `GET /auth/recovery/status` | 自动发送未配置与人工恢复说明 |
| `POST /auth/recovery/request` | 503，未发送任何邮件或消息 |
| `POST /admin/users/{user_id}/password-recovery` | reason、idempotency_key；201 返回 recovery_id、expires_at、首次 code、replayed |
| `POST /auth/recovery/complete` | email、code、new_password；成功要求重新登录 |
| `POST /admin/mfa/recovery-codes` | 返回只显示一次的十个 codes |
| `POST /admin/recovery-login` | email、password、recovery_code；独立管理会话 |
| `GET /auth/export` | section 为 profile/memberships/orders，limit≤200、cursor；返回 items/next_cursor |
| `GET /auth/closure` | 注销状态、未完成订单数、对账保留说明 |
| `POST /auth/closure` | password、confirmation_phrase 必须为“注销我的球讯账户”；返回匿名化或202待核对 |
| `POST /auth/closure/cancel` | password；取消申请，自动投注不恢复 |

`/auth/me`、`/admin/me` 增加 `reauthenticated_until` 和 `closure_requested_at`。增量结构为第八版；旧会话没有近期验证时点，迁移后要求重新验证，不默认视为刚验证。

## 冻结验证证据

```sh
docker run --rm --cpus=0.75 --memory=384m --network ai-football-v2-test-net \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e IDENTITY_TEST_DATABASE_URL=postgresql://postgres@ai-football-v2-pgverify:5432/postgres \
  -v /root/ai_football:/source:ro --entrypoint sh ai-football-api:v2.0.0-rc1 -c \
  'mkdir -p /tmp/lifecycle-frozen/tests && cp -a /source/services/api/app /tmp/lifecycle-frozen/app && cp -a /source/services/api/migrations /tmp/lifecycle-frozen/migrations && cp /source/tests/test_identity.py /source/tests/test_identity_lifecycle.py /tmp/lifecycle-frozen/tests/ && cd /tmp/lifecycle-frozen && python -m unittest discover -s tests -p "test_identity*.py" -v'
```

退出码 **0**，**46/46** 测试通过，**35.424 秒**，零跳过。包括原身份会员20项与新生命周期26项，每例创建具名独立临时数据库，先按有锁迁移初始化，测试结束删除该临时库；不使用生产连接、真实凭据或真实资金。首轮39项退出码0、29.908秒；增加会计证据、跨用户导出、管理闲置及审计失败后的43项退出码0、33.673秒。最后补充改密及恢复停投、旧会话并发拒绝、旧管理期限约束后，以本轮46项为最终结果。

验收覆盖：摘要存储、一次消费并发单胜、过期及旧码撤销、不同邮箱不能消费、幂等重放不再泄漏原码、账号及网络限流、管理员备用码消费轮换、CSRF、双身份、管理员 TOTP 防重放、旧验证窗口不能被刷新延长、过期仍能停止投注、未知和已接单证据保持不变、未发送订单取消、运行任务防止清凭据、匿名化后的稳定会计编号与凭据清空、跨用户数据隔离、键集分页和审计失败事务回滚。

已告知网页实现方集中处理重验，以及人工恢复/导出/注销页面契约。尚需主流程把新增路由纳入应用入口和统一测试镜像；网页与真实 HTTPS 操作需要独立验收，不能从服务端测试推断通过。
