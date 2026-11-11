# 本机对象存储切换记录（Stage B）

更新时间：2026-11-11 20:20 CET；基线 `fe0c66ae`，分支 `refactor/TASK-78-web-ux`。
责任目录 `infra/`（编排）、`docs/`（本记录）、`services/api`/`services/collector`（消费方只读核对）。
状态：**真实联调通过**。远端桶与远端主机尚未删除，回滚路径保留。

## 一、目标与边界

用户要求"完全本地化（之前的远程vps已经弃用），本机对象存储以空桶起步"。本轮把对象存储从远端 VPS 的 S3 兼容端点切到本机 MinIO，并从空桶载入当前仍在 72 小时留存窗口内、数据库仍在引用的媒体对象。

不做的部分：不删除远端桶、不删除远端主机数据、不清理本地已存在对象以外的任何历史；不改动 72 小时留存策略本身；不因切换放松图片读取的会员与授权校验。

## 二、为什么不能直接对空桶开切

`services/api/app/routes/expert_content.py` 的 `read_message_media` 把任何存储读取失败统一映射为 `503 远端图片暂不可读`。切换前统计：

| 项目 | 实测 |
| --- | ---: |
| 72 小时窗口内带媒体的消息 | 816 条 |
| 窗口内去重对象键 | 826 个 |
| 窗口内引用字节 | 约 62 MB |

因此"空桶起步"必须理解为**本地从空桶建立，而非直接空桶上线**：先把窗口内对象搬过去再切端点，否则页面上正在展示的原图会立刻变成 503。

## 三、本地 MinIO 布置

| 项目 | 取值 |
| --- | --- |
| 镜像 | `quay.io/minio/minio@sha256:a1ea29fa28355559ef137d71fc570e508a214ec84ff8083e39bc5428980b015e`（`RELEASE.2025-04-22T22-12-26Z`） |
| 运行身份 | `10001:10001`，`read_only`，`cap_drop: [ALL]` |
| 监听 | 容器内 `:9000` / 控制台 `:9001`，**不发布宿主端口** |
| 网络 | `ai-football-v2_storage-private` |
| TLS | 专用自签 CA + 叶证书，SAN `DNS:minio, DNS:localhost, IP:127.0.0.1`，3650 天 |
| 凭据 | `MINIO_ROOT_USER_FILE` / `MINIO_ROOT_PASSWORD_FILE` 指向只读挂载文件，不进入环境变量明文 |
| 资源 | `cpus` 0.5、`mem_limit` 512m、`pids_limit` 256 |

运行目录 `/root/ai_football/.runtime/minio-20261111/`，整体处于 `.gitignore` 的 `.runtime/` 之下：

- `minio.yml` — 服务定义
- `s3-local.yml` — 端点与凭据切换覆盖层
- `storage-tunnel-retired.yml` — 摘除远端隧道依赖
- `tls/`、`creds/`、`data/`（`data/` 为唯一持久卷目录）
- `local-storage.env` — 本机 MinIO 访问密钥，600 权限，不入库

`creds/` 与 `tls/` 初始为 root 所有，MinIO 以 10001 运行时报 `Unable to validate credentials inherited from the secret file(s)`；改为 `chown 10001:10001` 后正常启动。

## 四、对象搬运与校验

搬运脚本只读取窗口内键清单，逐对象比对**键尾内容哈希**：每个键的末段就是采集时算出的 sha256，因此复制后从目的桶读回再算一次哈希即可判定。结果：

| 项目 | 结果 |
| --- | ---: |
| 请求对象 | 826 |
| 复制成功 | 825 |
| 字节 | 62 266 605 |
| 哈希不符 | 0 |
| 失败 | 1（`NoSuchKey`，远端本已丢失） |

唯一失败对象 `v2/telegram/messages/-1002413688036/1632874/…` 有两条同键消息版本（`pending` 与 `ready`）。远端对该键返回 `NoSuchKey`，即**源侧本来就缺**，不是本次迁移造成。该对象无需伪造补齐，保留为已知缺口。

切换过程中又发现一个真实缺口：消息 212860 在导出键清单之后、端点切换之前上传到远端，首次搬运未覆盖。重新拉起隧道补拷 1 个对象（5 058 字节，哈希一致）后，窗口内 827 个键只剩上述 1 个远端本来缺失者。

最终本地桶：825 + 1 = 826 个对象（827 减去源侧缺失的 1），前缀全部为 `v2/telegram/messages`。

## 五、切换与隧道退役

覆盖层 `s3-local.yml` 把 `api`、`review-worker`、`collector` 的 `S3_ENDPOINT_URL` 指向 `https://minio:9000`，`S3_HTTPS_PROXY` 置空，`AWS_CA_BUNDLE` 改挂本机 CA。

两处踩坑，均已在覆盖层修正：

1. **`volumes: !override` 连带删掉了数据库 CA 挂载**。api 启动即 `FileNotFoundError`（asyncpg 加载 `sslrootcert`），健康检查失败。改为不覆盖、按目标路径合并，`/run/secrets/local-db-ca.crt` 与 `/run/secrets/s3-ca.crt` 现在并存。
2. **`AWS_CA_BUNDLE` 指到了叶证书而非 CA**，报 `unable to get local issuer certificate`。改指 `tls/ca.crt` 后建桶成功。

第三个问题不在覆盖层而在凭据来源：`S3_ACCESS_KEY`/`S3_SECRET_KEY` 来自 `.runtime/runtime.env`，仍是远端那套，切端点后 MinIO 返回 `InvalidAccessKeyId`。新增 600 权限、git 忽略的 `local-storage.env` 以 `--env-file` 注入，yaml 里只写变量引用，值不落文件。

`storage-tunnel-retired.yml` 做了三件事：清空 `api`/`review-worker`/`migrate` 的 `SSH_TUNNEL_HOST`；用 `depends_on: !override` 摘掉它们对隧道的等待；把 `storage-tunnel` 放进 `profiles: ["remote-storage"]`，使其不在默认 `up` 的启动集合内。确认数据库已走 `DATABASE_TRANSPORT=tls` 直连 `local-postgres`，隧道仅承载 S3 代理，摘除不影响数据库。

## 六、验收证据

| 检查 | 命令/方法 | 结果 |
| --- | --- | --- |
| 桶已建立 | head/create/head | `created` |
| 键覆盖 | 逐键 `head_object` | 827 检查，缺 1（源侧缺失） |
| api 读图函数 | 容器内 `blobstore.read_image` | 字节与 sha256 与库内记录一致 |
| review-worker 读图 | 同上 | 4 822 字节，sha256 一致 |
| collector 客户端 | 容器内 boto3 直连本地端点 | 4 822 字节 |
| 隧道停止后读取 | 停 `storage-tunnel` 后 `read_image` | 两张最新图均读回，哈希一致 |
| 默认启动集合 | `config --services` | 12 个服务，不含 `storage-tunnel` |
| 服务健康 | 容器状态 | api `healthy`，collector/review-worker `running` |
| HTTP | `curl` | `/healthz` 与 `/readyz` 均 200 |
| 公开赛事 | `/api/v1/public/matches` | 200 |
| 页面渲染 | Playwright 1440/390 | 10 行赛事，详情列显示"详情"，无告警条 |
| 采集恢复 | collector 日志 | 乐鱼与 Telegram 均返回 200，先前的 `ConnectError` 为 api 重建期间的瞬时中断 |

游客访问时 `/api/v1/auth/me` 与 `/api/v1/admin/me` 返回 401，属未登录的正常响应，与存储无关。

## 七、回滚与未完成项

回滚：移除 `s3-local.yml` 与 `storage-tunnel-retired.yml`，用原链路 `up -d --no-deps --no-build --wait api review-worker collector`，隧道回到默认集合，远端端点配置仍在盘上。远端桶未删除，可回读。

未完成：

1. 远端桶与远端主机上的对象数据未删除，远端 SSH 凭据未撤销。按顺序应在本地存储继续稳定运行后再做，且删除不可逆。
2. 媒体孤儿对象回收与 036 迁移未做；本地桶当前的 826 个对象全部在窗口内，尚无孤儿。
3. `media_orphans`、自动清理任务等与留存策略相关的代码项仍未实现，本轮只完成存储归属切换。

## 八、不影响本结论的已知限制

`tests/browser/score-summary-anchor.mjs` 默认连 `http://web`，该别名只在 `ai-football-v2_edge-private` 上存在，隔离测试网络里没有，因此它在多个历史提交上都同样失败，不构成本轮的回归信号，本轮证据未包含它。
