# 视觉重构上线记录（2026-11-12）

分支 `refactor/TASK-78-web-ux`，上线提交 `f0dd1c8c`。本文只记录可复核的事实与命令，不复述设计意图（设计见 `DESIGN_BRIEF_VISUAL_20261112.md`）。

## 一、上线内容

自基线上次刷新（`f66e6500`，2026-11-11 19:37）以来，`app.css` 累计 3073 行变更、`app.js` 78 行变更，由 8 个提交完成：令牌与外壳 → 比分页与推荐卡 → 专家/会员/个人中心/管理 → 间距收敛 → 视觉基线。本次上线的是这批改动的最终状态。

本次提交 `f0dd1c8c` 另含三处测量后修正的排版缺陷：

| 缺陷 | 实测证据 | 修正 |
| --- | --- | --- |
| 推荐卡正文下方空 31.9px | 1440/1030 卡高 113.4px；元信息 `grid-column:2/-1` 自成一行，卡高由 44px 按钮决定 | 元信息并入 `.recommendation-body` flex 列、`margin-top:auto`；卡高 89.5px，赔率→元信息间距 0、元信息→卡底 9px |
| 窄屏卡多 8px 空隙 | 390 卡高 206.4px，`priceToMeta` 8px | 窄屏 `margin-top:0`，间距由 `padding-top` 提供；卡高 198.4px，与桌面口径一致 |
| 筛选栏折成两行 | `#league-filter` 包在 `<label>` 内，`.filter-row > .small-button` 的子选择器够不到；360/390 实测 46px | 改后代选择器 `.strip-filters .small-button` 并 `white-space:nowrap; flex:0 0 auto`；26.5px |
| 选中日期被裁 | 390 裁 22px、360 裁 52px，`scrollLeft:0` | 只对 `#date-buttons` 计算 `scrollLeft` 居中；390 下 `scrollLeft:79`。不用 `scrollIntoView`——它会连带滚动文档，把 Chromium 的 Tab 起点挪到当前视口，首个 Tab 跳过页首「跳至主要内容」 |

## 二、验证证据

全部在 Docker 内执行，共 5 个浏览器镜像任务；宿主机只做 Docker/Git/文本。

| 检查 | 结果 |
| --- | --- |
| `verify.mjs` | 68 PASS / 0 FAIL |
| `verify.mjs --a11y` | 30 扫描、23 检查、0 违规、0 浏览器错误 |
| `homepage-expert-matches.mjs` | 1440/1030/390 全通过 |
| `manual-bet.mjs` | `ok:18` |
| `basketball-score-display.mjs` | 2/2 |
| `sports-expert-statistics.mjs` | 2/2 |
| `first-screen-requests.mjs` | 10/10 |
| `homepage-account-history.mjs` | 12/12 |
| `visual-check.sh`（比对模式） | 像素阈值自检 + 14 屏比对 + 盈利专家控制组 三项全 PASS |

基线更新说明：

- 14 张 `visual-baselines` 全部由当前源码重新渲染，逐张人工复核后才写入；fixture 摘要 `59c897f5…`（含一条篮球：三位数比分 + 半场比分 + 让分盘口）。
- `profitable-expert-baselines` 同步更新：1440 截图由 797×100 变为 385×137。原因不是回归，而是 `#account-content` 改为两列网格后该控件在列内重排。控制组断言（点击行为、本人确认、限额保留、手动回滚）全部继续通过，像素比对仍能检出人为注入的变更（`VISUAL_PROBE_MUTATION`）。
- 连续两次用同一源码生成基线，在 `channel_threshold:16` 下 14 屏差异像素均为 0，说明截图本身是确定性的；换用不同网络环境仍可能产生阈值以下的抗锯齿差异。

## 三、上线

- 镜像：`web/app/Dockerfile` 构建，digest `sha256:270af6e3d3d9c0b3124f738186ae77575109921888e386459cc283aae16718e2`，tag `ai-football-web:2.0.0-dev-t78ux-f0dd1c8c`。
- 候选覆盖：`.runtime/task78-visual-ux-20261112/candidate.yml`，只改 `services.web.image`。
- 命令：沿用 API web 容器 label 中读出的 176 层 compose 链 + 本候选，`docker compose --project-name ai-football-v2 --project-directory /root/ai_football --env-file …（4 份 0600 运行时 env + nov11-local-db/runtime.env）-f … up -d --no-deps --no-build --pull never --force-recreate --wait --wait-timeout 120 web`，退出 0。
- 仅 web 重建，其余 10 个服务容器未变动。

| | 镜像 | 容器 | 健康 |
| --- | --- | --- | --- |
| 部署前 | `sha256:0c39e031…` | `e7884ecd…` | healthy，restart 0，CSS 2496 行 |
| 部署后 | `sha256:270af6e3…` | `bf3b065a…` | healthy，restart 0，CSS 3361 行，含 `recommendation-body` 2 处、`strip-filters .small-button` 1 处 |

外部核验（`https://football.5545.eu.cc`）：

- `/` 200 / 49129B / 0.17s，`/assets/app.css` 200 / 72079B / 0.07s，`/assets/app.js` 200 / 115284B / 0.08s。
- 真实浏览器（Chromium 141，1440/390）打开线上比分页：24 行比赛、无横向溢出、无 JS 异常；390 下日期条 `scrollLeft` 自动为 79。两个 401 来自游客访问 `/auth/me`，属预期。

## 四、遗留

1. 个人中心 1440 下两列网格行高不均：`我的乐鱼账户` 465px 与 `自动投注与限额` 816px 同排，前者下方空约 351px。测过三种方案（多列流 `column-count`、整行跨列、单列全宽），均改善空档但会改变既有分区顺序或增加滚动长度，未纳入本次上线。
2. 前端资源没有内容指纹（文件名不含哈希）。`ETag`/`304` 本身可用：nginx 发 `ETag: "6af55c9d-1198f"` 与 `Last-Modified`，带 `If-None-Match` 或 `If-Modified-Since` 均返回 304、0 字节。问题在边缘缓存，已复现为上线缺陷，详见 `HANDOFF_VISUAL_FRONTEND_20261112.md` 第 3 节。
3. 主检出 `/root/ai_football` 仍在 `fix/TASK-78-hermes-telegram-sync` 分支且带他人未提交改动（`account_jobs.py`、`account_protocol.py`、`worker.py`、`test_account_protocol.py`）；本次上线的前端源码来自 `refactor/TASK-78-web-ux`，两条分支尚未合并，需在合并时统一。
