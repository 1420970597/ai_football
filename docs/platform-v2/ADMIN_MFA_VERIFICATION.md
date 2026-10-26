# 管理员验证器换机：冻结验证记录

更新时间：2026-10-26 12:01 UTC。状态：服务端隔离验证通过；网页整合和部署验收由主代理接续，不作为真实管理员换机成功证据。

## 目标与边界

支持管理员换手机或丢失旧验证器后重新登记 TOTP。开始和确认登记均要求独立管理会话、CSRF 和五分钟内凭据验证；通过密码加原 TOTP 登录/再次验证，或密码加一次性备用码恢复登录可获得该近期验证。单纯持有过期的近期验证状态不能改变因子。

旧验证器在新 TOTP 校验成功前保持有效。待确认秘密只在登记首次响应返回，远端数据库使用应用加密保存，并绑定发起的管理会话；十分钟过期。丢失首次响应时，明确替换待确认登记才能重新显示新秘密。查询状态不会再次披露秘密。

确认时在一个事务内替换因子、消费当前新 TOTP、清空待确认秘密、撤销其他管理会话、撤销旧备用码、创建十个仅返回一次的新备用码并写审计。当前管理会话保留，普通用户会话不受影响。重复确认只返回已经成功，不重复返回代码；响应丢失时，可通过保留的近期验证会话重新生成备用码。确认中途审计写入失败将整个事务回滚。

用户锁、会话锁、待确认登记锁采用固定顺序；用户使用不修改主键的行锁，兼容并发退出审计的外键检查。登录及管理再次验证对原密钥密文执行比较与交换，阻止换机前读到旧密钥的请求在换机后完成。取消未确认登记允许超过五分钟验证窗口，但仍要求有效管理会话和 CSRF，且不改变原验证器。

所有测试使用无真实身份的隔离 PostgreSQL，未发送邮件、Telegram 消息或真实投注。没有读取项目外配置。

## 接口契约

路径均以 `/api/v1/admin/mfa` 为前缀。

| 方法与路径 | 请求 | 响应重点 |
| --- | --- | --- |
| GET `/status` | 独立管理会话 | `configured`、`configuration_state`（configured/missing/unreadable）、`recovery_codes_remaining`、当前会话 `pending`（enrollment_id/expires_at 或 null）；没有秘密 |
| POST `/enrollment` | `{replace_existing:false}`，CSRF，近期验证 | 201：enrollment_id、secret、otpauth_uri、expires_at、message；秘密仅首次响应返回；已有未过期待确认登记且未明确替换时 409 |
| POST `/enrollment/{id}/confirm` | `{totp_code:"123456"}`，CSRF，近期验证 | configured、already_confirmed、recovery_codes（首次十个，重试 null）、other_sessions_revoked、message |
| DELETE `/enrollment/{id}` | CSRF，有效管理会话 | cancelled、previous_factor_unchanged；不需要重新输入已过期的近期验证 |

无效新验证码为 401，过期、撤销或原因子已改变为 409，其他会话的登记为 404，待确认加密内容不可读为 503。开始登记与确认各限制每账户十五分钟五次，并有网络级限制；达到限制返回 429 和 Retry-After。需要再次验证时沿用 `403 detail.code=reauthentication_required`。

确认用过的 TOTP 已被消费，立即用同一枚验证码登录将被拒绝；下一枚验证码或新的备用码可以登录。新备用码应当离线保存，不写浏览器持久存储。新秘密和备用码不得出现在日志、审计或错误详情。

## 冻结与实际命令

先复制应用、迁移与三个身份测试模块到任务容器内，再执行验证。运行过程没有修改冻结副本。

```sh
docker run --rm --name ai-football-v2-mfa-test --cpus=.75 --memory=384m \
  --network ai-football-v2-test-net \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e IDENTITY_TEST_DATABASE_URL=postgresql://postgres@ai-football-v2-pgverify:5432/postgres \
  -v /root/ai_football:/source:ro --entrypoint sh ai-football-api:v2.0.0-rc1 -c \
  'mkdir -p /tmp/mfa-frozen/tests && cp -a /source/services/api/app /tmp/mfa-frozen/app && cp -a /source/services/api/migrations /tmp/mfa-frozen/migrations && cp /source/tests/test_identity.py /source/tests/test_identity_lifecycle.py /source/tests/test_identity_mfa.py /tmp/mfa-frozen/tests/ && cd /tmp/mfa-frozen && python -m unittest discover -s tests -p "test_identity*.py" -v'
```

退出码 **0**，**63 项通过、零跳过，52.644 秒**。其中基础身份/会员二十项、恢复/再次验证/本人导出注销二十六项、新增换机十七项。每个数据库行为测试均创建并销毁唯一临时数据库，执行完整有序迁移（包括本次新增迁移）。项目代码只读挂载，容器 CPU 0.75、内存 384 MiB，没有暴露测试数据库端口。

新增十七项覆盖：待确认内容加密及不重新披露、错误验证码保留原因子、其他管理会话与旧备用码撤销、普通用户会话隔离、并发/重复确认幂等、发起会话归属、角色/CSRF/近期验证、过期验证仍可取消、登记过期、明确替换、并发原因子变更、损坏密文、审计失败完整回滚、丢失设备后备用码恢复换机、确认 TOTP 防重放、尝试限流、旧密钥并发登录、旧密钥并发再次验证。

`git diff --check`（仅本阶段相关模块）退出码 **0**。

## 本阶段源内容摘要

| 内容 | SHA-256 |
| --- | --- |
| 管理员换机模块 | b730fdbb8e02788a167d33c1c922428797a999b27068d11ba6368bc4d56602be |
| 身份模块（含原密钥 CAS） | 749291074ec14645eea32833a7fdaef12331f20dd4310edbd0bd5609b423356e |
| 生命周期模块（含原密钥 CAS） | 21dedefd10f666b7143ede3caf45186a5783540427b430c214cebe18b8764bc9 |
| 管理员换机增量迁移 | fad63391064ce1a6db96160a41788215ea0905f82a12dbf6efcd6fb0c7718011 |
| 换机行为验收 | e3e358972ced152eeb58b8406594928c7b3b55d6084dab3db9d3805f1f88aaf4 |

## 仍需整合

主代理接入独立路由和统一冻结回归；网页代理增加管理安全入口、内存中的二维码展示、到期/取消/显式替换、成功后新备用码一次展示。真实生产管理员授权换机、正式发布及签名不属于本次隔离验证。

## 后续冻结：统一撤销边界

更新时间：2026-10-26 12:08 UTC。前述 63 项记录是第一轮冻结证据，保留其原命令、结果及内容摘要。本节记录其后的同根因修复，当前可整合版本以本节为准。

复核发现，备用码生成若仅在事务前读取管理会话，可能在等待用户锁时已被另一会话换机撤销，随后仍生成新备用码。现将事务内用户锁、管理会话有效性与近期验证合并为共享校验；换机开始、确认、取消、备用码生成及人工签发普通用户密码恢复码均使用该校验。取消保持免近期验证，但撤销、过期或身份已停用的管理会话仍被拒绝。确认不会被排队的旧会话再次替换备用码。

使用上节同一 Docker 冻结命令再次执行完整身份验收，退出码 **0**，**70 项通过、零跳过，57.492 秒**。七项新增场景为：预检后被撤销的会话不能开始换机、不能确认、不能取消、不能生成备用码、不能签发用户恢复码；等待期间五分钟验证已过期时不能生成备用码；真实换机撤销另一会话后，另一会话先前已排队的备用码生成返回 401，且新备用码摘要保持不变。没有用假返回替代数据库事务。

本阶段 `git diff --check` 再次退出码 **0**。当前内容摘要：

| 内容 | SHA-256 |
| --- | --- |
| 管理员换机模块 | dc5b49acb6fdb03570e3f54a7559ddf7a66ecc25e9f62509ec76c5feb7a22ba5 |
| 身份模块（共享事务内管理会话核验） | 4a96c3d6f733eb7f782b7d6feb76ea13df3ecd0717510cbf57bbb2bc00e18b8c |
| 生命周期模块（备用码/人工恢复签发核验） | d6c6272524dc01cbbee9bd24953a383fc34acdac3fbb3bf8c1fd7a7be6d9f16b |
| 换机行为验收 | 53e86cef3807979e181ee7f4d2332b5caf32a36ccbb1f506f9a164d3f08040c1 |

增量迁移和接口响应契约未再变更。代码在本轮验证后再次冻结，等待统一套件和网页真实接口回归。
