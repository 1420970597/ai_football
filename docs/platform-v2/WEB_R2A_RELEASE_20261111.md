# 网页首屏请求预算 R2a：发布与验证（2026-11-11）

编号：TASK-78-R2A-FIRST-SCREEN-20261111。授权：用户 2026-11-11 “批准自动部署到生产环境与自动提交、合并远程仓库”。源码 `dcfc574e`（`refactor/TASK-78-web` 合并提交，已推送，远端引用已核对）。

状态口径：**已发布并做了只读后验**。没有手机真机验收，也没有在真实登录态下长时间观察重复请求。

## 1. 问题

合并集成分支前的一次浏览器回归里，`first-screen-requests.mjs` 的 `user-scores` 场景测得首页 VS 图标被请求 **2 次**，而断言只允许 1 次。同一棵树重跑 3 次均通过，r2a 自己的最终运行也通过 —— 属于间歇性。

根因（已定位，不是测试写错）：已登录状态下，手动下注配置加载完成后会再调用一次 `renderMatches()`（`web/app/app.js` 约 1451/1483 行），整个列表用 `innerHTML` 重建，包括每行的 `<img class="match-vs-icon">`。当第一张图尚未加载完就被替换成新节点时，浏览器会对同一 URL 再发一次请求。主机负载高、图片加载慢时竞态才暴露。

## 2. 修复

`adoptVsIcons()`：每次 `renderMatches()` 结束时，把上一轮已经在页面上、且加载成功（`complete && naturalWidth > 0`）的图标节点直接搬进新的插槽，而不是新建 `<img>`。`scoreMarkup` 不再写内联 `src`，由 `adoptVsIcons()` 统一赋值（含首轮赋值与加载失败回退）。同一个节点被移动而不是重建，浏览器不会重新请求。

## 3. 测试依据

- 新增两个确定性场景：`user-scores-slow-icon`（图标延迟 300 ms）与 `user-scores-late-settings`（配置接口延迟 700 ms），显式制造原来导致竞态的时序，断言图标请求恰好 1 次、`/accounts/leyu/manual-betting` 恰好 1 次。
- 合并结果上的浏览器套件：`first-screen-requests.mjs` 10/10 通过；`accessibility.mjs`、`verify`、`homepage-account-history` 均退出 0。测试容器复用 `ai-football-browser:v2.0.0-dev`，显式私有子网，未访问真实上游。
- 断言没有放宽：失败场景改的是实现，不是期望值。

## 4. 镜像

- 补丁式构建：`deploy-local/WebR2a.Dockerfile` = `FROM ai-football-web:2.0.0-dev-task78-f82a609a` + 覆盖 `web/app/app.js`、`web/app/admin.js`、`web/app/index.html`，`LABEL org.opencontainers.image.revision="dcfc574e…"`。回避未锁定的传递依赖漂移，与 11-11 早先一次发布的构建方式一致。
- 构建 `--network none --memory 512m --cpu-quota 50000`，退出 0 → 标签 `ai-football-web:2.0.0-dev-r2a-dcfc574e`，镜像 ID `sha256:aabba5bfa015dd5643152986f970f7f108bb7977fd275a9b4f95030443322557`。
- 离线校验：`--network none` 容器内三个文件的 md5 与冻结源码逐一相同；`nginx.conf` 与上一版逐字节相同（`35940baf…`），因此静态压缩与回退行为不变。

## 5. 执行记录（UTC）

| 时间 | 事件 |
| --- | --- |
| 15:54:52 | `web switch result=0`，`rollout_exit=0` |
| 15:54:56 | 容器内检查通过（healthz、`/assets/app.js` gzip、`/public/experts`、`/`） |

发布只替换 web 容器，未触碰 api、worker、数据库与资金开关。

## 6. 发布后证据（只读）

线上 web = `sha256:aabba5bfa015…`，running/healthy，重启 0。

主机端口实测（web 发布在宿主机 **3003**，`80/tcp -> 0.0.0.0:3003`）：

| 路径 | 状态 | 字节 |
| --- | --- | --- |
| `/healthz` | 200 | 33 |
| `/index.html` | 200 | 46522 |
| `/assets/app.js` | 200 | 105865 |
| `/assets/admin.js` | 200 | 46945 |
| `/api/v1/public/experts` | 200 | 11283 |

`/assets/app.js` 与 `/assets/admin.js` 在 `Accept-Encoding: gzip` 下均返回 `Content-Encoding: gzip`，带 `ETag` 与 `Last-Modified`。

### 证据的限度与一次更正

首次主机侧验证报告 `/assets/app.js` 404（153 字节）、`/api/v1/public/experts` 404 —— 那是**探错了端口**：宿主机 8080 上是另一个 docker-proxy 后端，不是本站点；发布脚本自身在容器内的检查一直是通的，`nginx.conf` 也与上一版逐字节相同。改在 3003 复测后全部 200。这一条保留在文档里，因为它是本次唯一的验证误报。

未做：手机真机首屏观感、弱网下连续刷新 10 次以上的重复请求统计、CDN/代理层缓存行为。

## 7. 回滚（未执行）

```
R=/root/ai_football/.runtime/task78-r2a-20261111 \
WIMG_FILE=/root/ai_football/.runtime/task78-r2a-20261111/rollback.yml \
bash deploy-local/rollout-web.sh
```

`rollback.yml` 指向 `sha256:ab30da9056d579fc7caafa8569d2c02048ed33cb479e06a4d256755e77894298`（11-11 早先发布的 web 镜像，仅含 `nginx.conf` 压缩改动）。

发布件位于 `.runtime/ux-hardening/deploy-local/` 与 `.runtime/task78-r2a-20261111/`（不入库）。

## 8. 发布链注意事项

本地化阶段 A 之后，worker 的启用来自覆盖文件 `.runtime/task78-localize-20261111/workers.yml`。任何 web 发布都必须走带该覆盖的链（`localization/compose-now.sh`，或 `compose-base.sh` 再加 `-f .runtime/task78-localize-20261111/workers.yml`），否则会把采集器/执行器退回远端安排。
