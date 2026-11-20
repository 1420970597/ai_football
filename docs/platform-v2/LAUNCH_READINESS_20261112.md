# 上线状态判定与上线动作记录（2026-11-12）

分支 `fix/TASK-78-hermes-telegram-sync`。用户指令：「新版系统是否已经具备上线状态？如果具备第一时间上线，无需考虑抽取准确性」。本文件记录判定依据、本轮执行的上线动作、以及判定当时仍未解决的风险。抽取准确性按用户指示不构成本次上线门槛。

## 一、判定结论

判定：**已上线，公网可用**。`https://football.5545.eu.cc` 由 Cloudflare 前置、主机 nginx 回源 `127.0.0.1:3003` 到 `ai-football-v2-web-1`，是本机生产入口；本轮之前即已在服务，本轮补充了发布保底与链路加固。

不满足「完整交付」定义的部分（见第十五节完成定义）单列于第五节，不因上线而视为已验收。

## 二、运行状态证据

容器（项目内 11 个，全部无重启重启计数为 0）：

```
web            healthy     api            healthy     postgres   healthy
raw-retention  healthy     local-mcp-api  healthy     hermes     healthy
collector      running     review-worker  running     executor   running
hermes-mcp     running     minio          running
```

镜像为同一批次构建，运行中容器实际使用的摘要：

| 服务 | 镜像摘要 | 构建时间 |
| --- | --- | --- |
| web | `sha256:0c39e031…f5778` | 2026-11-12 02:16 |
| api / review-worker | `sha256:928e9800…dd37a` | 2026-11-12 02:05 |
| collector | `sha256:710c6825…3afa0` | 2026-11-12 01:45 |

`web` 镜像内容与公网一致：`grep -c '配置金额后实盘买入'` 在公网域名下返回 1。

## 三、公网可用性与安全

| 检查 | 结果 |
| --- | --- |
| `GET /healthz` | 200，0.089–0.16 s |
| `GET /`（桌面 1440 / 手机 390 真实 Chromium） | 200，16 条赛事行，0 pageerror，0 失败请求，加载 3.5–3.8 s |
| 静态资源 `app.js` / `app.css` / `ui.js` / `identity.js` / `admin.js` | 全部 200 |
| `GET /api/v1/public/matches?recommended_only=true` | 200，10 场 / 22 条推荐 |
| `GET /api/v1/public/experts` | 200，14 位专家含统计与 `sample_state` |
| 匿名 `GET /api/v1/admin/*` | 401 |
| 匿名 `PUT/POST /api/v1/accounts/leyu/manual-betting*` | 401「需要登录」 |
| `POST /api/v1/auth/register` 空体 | 422 字段校验（非 500） |
| `POST /api/v1/auth/login` 错误凭据 | 401 |
| 响应头 | `nosniff`、`referrer-policy: same-origin`、`X-Frame-Options: DENY`、CSP 含 `frame-ancestors 'none'` |

## 四、本轮执行的上线动作

### 4.1 迁移 037 之后的首个数据库转储

此前最新完整转储为 2026-11-11 03:36（`final-v2.dump`，515 MB）与 14:48 的迁移前转储，均早于迁移 037。现补充：

```
docker exec ai-football-v2-local-storage-local-postgres-1 \
  pg_dump -U football_admin -d football_v2 -Fc -Z6 \
  > .runtime/launch-20261112/football_v2-post037.dump
```

退出码 0，产物 102072103 字节，`pg_restore --list` 可读（424 项、53 张表数据），
sha256 `93bbbaa36eda395eca04f910195e561327b9a52af13c48ba65e584795780f0b5`。
该文件含生产数据与密文列，权限 `0644` 位于 `.runtime/`（不纳入版本库），不得外传。

### 4.2 主机 nginx：HSTS 与访客侧 HTTPS 升级

文件 `/etc/nginx/sites-enabled/football.5545.eu.cc.conf`，备份在
`.runtime/launch-20261112/football.5545.eu.cc.conf.bak-20261112`。

1. 增加 `add_header Strict-Transport-Security "max-age=15552000" always;`，不加 `includeSubDomains`（本域已是子域，其下无我方服务）。
2. 增加按**访客协议**判断的升级：

```nginx
if ($http_x_forwarded_proto = "http") {
    return 301 https://$host$request_uri;
}
```

不使用 `$scheme`：Cloudflare 目前以 HTTP 回源，若按回源协议判断会形成 301 环。

`nginx -t` 通过，`systemctl reload nginx` 退出码 0。验证：`strict-transport-security: max-age=15552000` 已出现；HTTPS 访客 200 且 `hops=0`（无重定向环）；`/`、`/assets/app.js`、`/assets/app.css`、`/healthz` 均 200；加固后重跑真实浏览器渲染仍为 16 行、0 错误。

## 五、上线时仍未解决的风险（不得视为已验收）

### 5.1 免费套餐携带自动投注权益（真实资金相关，最高优先）

唯一套餐 `free`（「游客与免费用户」，启用，3650 天，价格 0）的权益为：

```json
{"expert_summary": true, "public_fixtures": true, "automatic_betting": true,
 "expert_full_history": true, "expert_live_signals": true}
```

注册即自动授予该套餐（`services/api/app/auth.py:266`）。`automatic_betting` 是
`orders.manual_policy()` 与 `orders.account_policy()` 的实际资金门槛，因此**任何完成乐鱼账户校验的注册用户都获得实盘资格**。现有用户 `aaa1111` 名下另有两条 active 的该套餐（系统授予至 2126-10-29、管理员授予至 2129-07-24）。

本轮未修改该数据：调整它会改变真实资金准入，属于只有所有者能决定的事项。

### 5.2 实盘双开关为开启状态

```
live_enabled(settings) = True          # LIVE_ORDERS_ENABLED=true
global_enabled(conn, settings) = True  # v2_admin_settings.auto_betting_enabled 解密为 true
```

`v2_bet_orders` 中已有 22 条 `settled`，均带乐鱼真实订单号（如 `5382110238447533`），
合计本金 1055 元、`provider_reported_net` 合计 −202.5 元；另有 11 条 `cancelled`（本金 550 元，未提交）。这些是真实资金成交，不是样本。上线后新推荐会继续进入该通道，历史上限为 `stake_per_order=10`、`max_per_order/daily_limit/open_exposure_limit=1000`、`auto_enabled=true`、`expert_selection_mode=profitable`、9 位允许专家。

### 5.3 采集侧：Telegram flood 退避与事件 409

上线核验期间采集器近 20 分钟记录 255 次
`Telegram source backoff request=GetMessagesRequest`，以及 33 次
`POST /events` 返回 409 `version_content_conflict`，并把该来源标记为
`Telegram source unavailable category=HTTPStatusError`。

已确认的部分：

- 数据没有中断：近 20 分钟入库 33 条事件、2 个来源活跃，`v2_current_quotes` 最新报价滞后 <1 秒，5 分钟 6017 行。
- 相册展开不是主因：近 20 分钟 `album_expansions` 仅 4 次。
- 409 的来源是 `v2_telegram_events` 中同 `version_key` 已存在且内容（除昵称外）不同，见 `services/api/app/routes/telegram.py:384`。

未确认的部分：这些 409 是重复提交的正常幂等拒绝，还是采集器重建了与已存版本不一致的 payload（后者意味着该版本的消息内容未按新内容落库）。需要按 `platform_message_id` 对照采集器提交体与库内 `content` 才能定性。本轮未修改该路径。

同时存在 9 条 `pending` 媒体事件（均为 `is_current=false` 的历史版本，最早 2026-11-09），`/internal/telegram/pending` 只取 `is_current`，因此它们不会被重试，也不会阻塞当前版本。

另：`Leyu poll state=partial` 每 8–12 秒出现一次（`last_success_at` 持续前进），与报价新鲜度不受影响一致，属部分子步骤失败而非整轮失败，本轮未细究。

### 5.4 其余已知缺口

1. **无当前 Android 候选包**：`android/build/artifacts/` 最新为 2026-10-28 的 dev 包（40 KB），与当前提交不对应。
2. **`ai-football-v2-pgverify` 仍在运行**：2026-10-26 启动，`--restart=no`，挂载卷 `a31185d3…`。它不是本项目业务容器，属遗留验证用途。
3. **磁盘 85%**（37 GB 可用）。`.runtime` 占 28 GB，其中 `perf-backup-20261028` 6.4 GB、五个 `ai-football-e*-restore-data` 卷约 16.4 GB 待决。
4. **`unsupported` 媒体门**：`telegram.py:338` 的 `media_state == "ready"` 使媒体被判为不支持的 313 条/72h 消息永不进入审核；其中 312 条无正文，1 条为赌博广告，当前不误伤真实推荐。
5. **约 2000 条历史 `unresolved` 审核任务**（最早 2026-10-29）未批量重放。
6. **工作树有他人未提交修改**：`services/api/app/account_jobs.py`、`services/executor/account_protocol.py`、`services/executor/worker.py`、`tests/test_account_protocol.py`，本轮未触碰。
7. **历史密钥暴露**：某次历史会话中 `S3_SECRET_KEY` 曾被回显到会话记录，建议轮换该密钥（本文件不复述其值）。

## 六、复现命令

```sh
# 容器与健康
docker ps --filter 'name=ai-football-v2-' --format '{{.Names}}\t{{.Status}}'

# 公网
curl -sS -o /dev/null -w '%{http_code} %{time_total}s\n' https://football.5545.eu.cc/healthz
curl -sSI https://football.5545.eu.cc/ | grep -iE 'strict-transport|content-security|x-frame'

# 数据新鲜度
docker exec ai-football-v2-local-storage-local-postgres-1 psql -U football_admin -d football_v2 -c \
  "SELECT now()-max(received_at) AS lag FROM v2_current_quotes;"

# 实盘开关（在 api 容器内，不输出密钥）
docker exec -i ai-football-v2-api-1 python - <<'PY'
import asyncio, sys; sys.path.insert(0,'/app')
from app import database, orders
from app.config import load_settings
async def main():
    s = load_settings(); await database.open_pool(s)
    async with database.current_pool().acquire() as conn:
        print('live_enabled=', orders.live_enabled(s))
        print('global_enabled=', await orders.global_enabled(conn, s))
    await database.close_pool()
asyncio.run(main())
PY
```
