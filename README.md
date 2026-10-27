# ai-football Platform v2

基于乐鱼赛事与盘口数据、Telegram 专家来源和 Hermes 审核流程的足球信息平台。项目以 Docker Compose 运行，业务数据保存在远端 PostgreSQL 与 S3；本机提供 Web 展示和管理界面。

## 启动

运行环境配置由受保护的 `.env` 指向 `.runtime/runtime.env`。启动前需准备 Docker Compose、存储隧道、PostgreSQL、S3、Hermes 网关和 Telegram/乐鱼运行配置。不要把 `.runtime/` 中的秘密提交到 Git。

```sh
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:3003/healthz
```

Web 默认绑定 `0.0.0.0:3003`，可通过 `http://<主机地址>:3003` 访问。修改端口或绑定地址时使用 `WEB_PORT` 与 `WEB_BIND_ADDRESS`。

管理员账号由受保护运行配置中的 `BOOTSTRAP_ADMIN_EMAIL` 和 `BOOTSTRAP_ADMIN_PASSWORD` 引导创建。已存在的管理员不会被启动配置覆盖；更改现有账号须通过受控身份操作完成。TOTP 仅在管理员账号启用时要求输入。

## 服务与安全边界

- `web`：静态 Web 客户端，监听宿主机 3003。
- `api`：身份、比赛展示、专家记录、管理与审计 API。
- `collector`：乐鱼及 Telegram 数据采集；Telegram 登录态由管理员配置并加密保存。
- `review-worker`、`hermes`、`hermes-mcp`：异步内容审核及受控工具调用。
- `storage-tunnel`：连接远端 PostgreSQL、S3 和外部服务的受限网络入口。
- `executor`：独立订单执行进程；真实投注默认关闭。

检查页面、API 与后台进程应分别查看容器健康状态和管理页中的上游状态。容器运行或 API 返回 200 不等于 Telegram 授权、乐鱼采集或实时盘口已经验收。

## 开发与验证

Python、数据库、浏览器和 Android 验证均在 Docker 中执行；不要在宿主机安装项目依赖。完整项目约束、任务拆分和验证台账见 [AGENTS.md](AGENTS.md)、[TODO.md](TODO.md) 与 [docs/platform-v2/PROGRESS.md](docs/platform-v2/PROGRESS.md)。

```sh
sh infra/test/run.sh
sh tests/browser/check.sh
sh tests/browser/integration-check.sh
sh tests/browser/accessibility-check.sh
sh infra/check-runtime.sh
```

Telegram 来源可以同时监控多个群组，自动同步管理员及已信任作者的最新昵称。目录搜索可切换“所有成员 / 仅管理员”；选择成员后才加入监控，不会自动信任所有群成员。群成员不可枚举时保留明确原因及手动编号入口。

管理设置中的模型与报价有效期按运行配置生效：模型在下一次审核任务中经 Hermes 框架调用，报价阈值在后续请求中读取。消息图片保留类型、字节数、哈希与解码校验；错误不能变成空图片成功。订单中的预留本金/赔率和官方成交字段分别展示，缺少官方证据显示未知。

Web 与 API 使用独立入口网络；默认 `EDGE_SUBNET=10.79.253.0/28`，Web 地址 `.2`、API 地址 `.3`。如与既有 Docker 网段冲突，须同时设置 `EDGE_SUBNET`、`EDGE_WEB_IP`、`EDGE_API_IP`。API 仅信任该 Web 地址，入口覆盖外部转发头；不得配置通配信任。签名及发现配置采用专用只读组，仅采集与执行器获得该组。

当前为开发候选。正式签名、设备安装升级、官方赛果及生产验收条件未满足时，不发布稳定版本。最近一轮历史整改、真实测量、失败及剩余依赖分别记录于 [采集核验](docs/platform-v2/HISTORICAL_COLLECTOR_AUDIT.md)、[结算统计核验](docs/platform-v2/HISTORICAL_SETTLEMENT_AUDIT.md)、[身份配置核验](docs/platform-v2/HISTORICAL_IDENTITY_AUDIT.md)、[存储恢复核验](docs/platform-v2/HISTORICAL_STORAGE_AUDIT.md) 与 [Android 核验](docs/platform-v2/HISTORICAL_ANDROID_AUDIT.md)。
