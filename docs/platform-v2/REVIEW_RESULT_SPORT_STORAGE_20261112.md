# 专家发言识别：结果车道、体育项目与本地对象存储修复（2026-11-12）

分支 `fix/TASK-78-hermes-telegram-sync`。本轮处理三项缺陷，全部在 Docker 内验证并部署到本机生产。

## 一、结果核验车道无限重试与无节流（缺陷 B2）

现象与度量（修复前）：

| 原因 | 行数 | 最大失败次数 | 已有终局结果的行 |
| --- | --- | --- | --- |
| `result_detail_unavailable` | 346 | 775 | 315 |
| `result_fixture_mismatch` | 82 | 370 | 82 |
| `upstream_rate_limited` | 58 | 207 | 57 |

315/347、82/82、57/57 这些行**已经拿到确认终局**，只是还在盯改判，却仍按 16 分钟上限反复重读；单场最高 775 次。这些流量本身就是上游限流与盘口刷新被阻断的来源。

修复：

1. `services/collector/leyu_results.py`：`ResultCollector` 增加 `RESULT_READ_SPACING_SECONDS = 2.0` 的读取间隔，与站点客户端盘口读取的 2 秒下限一致。此前该车道无间隔、并发发出两次读取。
2. `services/api/app/routes/result_checks.py`：`finish_check()` 按“是否已有确认终局”分成两条退避阶梯：
   - 未终局（结果仍欠专家台账）：`LEAST(1800, 30*2^min(failures,5))`，上限 30 分钟。
   - 已终局（仅盯改判）：`LEAST(21600, 300*2^min(failures,6))`，5 分钟起、上限 6 小时。

部署后核对：已终局行的下次检查排到 320 分钟后，未终局行 16 分钟；API 与采集器健康。

## 二、体育项目污染（缺陷 C）

五个上游 csid 无映射，1811 行落为 `source_3/6/8/9/37`；且 `7` 被标为 baseball，但它产出的每一行都是 冠中冠（奥沙利文、特鲁姆普、罗伯森、墨菲），实为斯诺克；真正的棒球联赛走的是 csid 3。

按联赛名称实测重映射（`services/collector/leyu_protocol.py`）：`3` baseball、`6` american_football、`7` snooker、`8` table_tennis、`9` volleyball、`37` cricket；未识别 id 保留显式 `source_N`。

协议映射只影响上游仍在上报的行：赛事 upsert 会比较内容并跳过未变化行，已完赛的行保留首次写入的标签。因此新增迁移 `037_correct_legacy_sport_labels.sql`，按行内已有的联赛名称做确定性校正，只改 `sport` 字段：

- `source_8→table_tennis`、`source_9→volleyball`、`source_37→cricket`、`source_6→american_football`、`source_3→baseball`
- `baseball` 中联赛为 冠中冠/冠中冠杯 的 9 行 → `snooker`

写迁移前核实：这些赛事没有专家信号引用（计数 0）；报价以 `fixture_id` 为键，不受标签影响。

结果（迁移 037 已应用）：

```
legacy_tokens=0  snooker_as_baseball=0
football=10875 basketball=1667 table_tennis=1538 tennis=1342 badminton=372
ice_hockey=195 volleyball=159 cricket=49 american_football=32 baseball=26 snooker=12
```

命令与退出码：

```
sh infra/test/run.sh tests.test_migrations      → Ran 9 tests, OK, exit=0
compose run --rm --no-deps migrate              → applied_count=1
```

## 三、媒体上传仍指向已弃用远端存储

现象：真实推荐「二节 奇才+0.5」（2026-11-12 00:36:08）有 `v2_telegram_events` 行，但 `v2_analysis_jobs` 为 0 条，事件 `last_error='ProxyConnectionError'`、`media_state='failed'`。

根因两层：

1. `api`、`collector`、`review-worker` 仍使用 `S3_ENDPOINT_URL=https://162.35.188.113:9000` 且 `S3_HTTPS_PROXY=http://storage-tunnel:18080`，而 `storage-tunnel` 容器已不存在、远端 VPS 存储已停用。
2. 挂载的 `/root/ai_football/.runtime/s3-ca.crt` 是远端旧 CA（前缀 `201c1e1bae19b4fd`），本机 MinIO 的 CA 是 `5485209013a778cc`，即便换端点 TLS 也会校验失败。

修复：把本机 MinIO 接入运行链。新增 `.runtime/task78-local-store-20261112/compose.sh`，在原 r7c 覆盖链后追加：

- `.runtime/minio-20261111/s3-local.yml`：端点 `https://minio:9000`、`S3_HTTPS_PROXY=""`、`AWS_CA_BUNDLE=/run/secrets/s3-ca.crt`，并把本机 CA 绑定到该路径
- `.runtime/minio-20261111/storage-tunnel-retired.yml`：`SSH_TUNNEL_HOST=""`、隧道服务转 profile
- `--env-file .runtime/minio-20261111/local-storage.env`：该存储自身的密钥对（不写入仓库）

合并后核对的生效配置（三个服务）：`S3_ENDPOINT_URL=https://minio:9000`、`S3_HTTPS_PROXY` 空、CA 源为本机 `minio-20261111/tls/ca.crt`。

验证：

- api 容器内用 `blobstore._s3_client()` 对本机 MinIO 做 put/get/delete 往返 → `roundtrip b'local-store-ok'`
- 重启后采集器日志连续出现 `Telegram media event commit state=ready`（含 1–2 张图）
- 之前失败的消息 `11c11d23-…` 经 `retry_pending`（该接口同时纳入 `failed`）重传成功：`media_state=ready`，并生成审核任务

## 四、顺带修正的部署漂移

- `review-worker` 原先跑的 r7c 镜像早于迁移 036，启动即 `MigrationRequired` 退出。它与 api 同 Dockerfile 同镜像，现统一钉到同一 digest。
- `migrate` 服务同样需要新镜像：旧构建不认识 036，会把整库判为“未知版本”而拒绝执行。

候选覆盖 `.runtime/task78-sport-and-results-20261112/candidate.yml` 中 api/migrate/review-worker 共用 `sha256:928e9800…dd37a`，collector 为 `sha256:710c6825…3afa0`。

## 五、仍需处理

1. `services/api/app/routes/telegram.py:338` 的 `media_state == "ready"` 门槛：媒体上传失败的消息完全不会进入审核。实测被拦住的 3 条均为 2026-11-09 的赌博广告（无真实推荐），当前不误伤；但设计上应保留“原文已收到、媒体失败”的可审核路径，需要连同 §九.6（不把不完整消息送模型）一起设计。
2. 队列仍有约 2000 条 `unresolved`（最早 2026-10-29），主要来自历史回放与确实未匹配的闲聊；本轮未批量重放。
3. Hermes 上游（`152.53.126.151:8885`，`deepseek-v4.1-flash`）在排查期间出现 503，12 次审核尝试失败，随后恢复；属外部依赖。
