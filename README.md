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
