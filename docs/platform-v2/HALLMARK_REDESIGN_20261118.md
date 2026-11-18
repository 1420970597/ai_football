# 球讯界面重构

更新时间 2026-11-18 02:24 UTC。当前目标为重构网页与 Android 壳的页面布局、视觉系统及交互状态。当前分支 `fix/TASK-78-hermes-telegram-sync` 与远端起点均为 `97ab3a5d`，工作树中的收益研究和交接修改归原任务。

| 工作包 | 责任目录 | 前置与当前证据 | 设计与实现 | 验收及失败处理 |
| --- | --- | --- | --- | --- |
| 比分与详情 | `web/app/` | 已有捷报、CC 观察和 Docker 固定样本。缺少本轮前后对照 | 桌面紧凑赛事表、移动分层比赛行、统一赛程赛果筛选、深色比分详情 | 多宽度查赛、筛选、关注、盘口、旧内容刷新及无数据。保留失败记录，修复后重跑 |
| 专家、会员与账户 | `web/app/` | 原路由和界面已盘点，既有角色、表单和授权逻辑正常入口用于验证 | 专家统计与原文区分层，会员套餐与权益并排，账户按资料和交易职责组织 | 双端统计、证据、套餐、登录和账户隔离。用隔离样本覆盖写流程 |
| 独立管理与客户端 | `web/app/`、`android/` | 已有独立会话、管理分区、WebView 和原生配对。当前无本轮真机证据 | 管理工作区与公共比分分开，原生工具栏与离线／配对页面同步视觉结构 | 独立登录与管理分区用既有 Docker 测试，Android 用实际构建及可用设备核验 |

## 2026-11-18 02:48 UTC，隔离验证通过

- 比分桌面保持普通行不超过 45px，手机普通行不超过 80px。宽屏容器上限 1376px，标题 28px，手机比分标题 21px，钴蓝用于筛选和主操作，琥珀用于深色比分牌。实盘金额设置按需展开。原比赛、盘口和证据读取路径沿用现有接口。
- 专家统计筛选与证据入口分层。会员的套餐和权益并排呈现，窄屏堆叠。个人资料与登录安全并排，账户、订单等业务设置独立展开。管理区宽屏采用分区侧栏，移动端采用横向分区导航。
- 复用捷报研究的查赛流程、赛事与专家并列及手机五项底栏。CC 5.5.2/2552 许可未核验的两个静态图保留在历史源码，运行替代为文字 VS 和 CSS 提示符，候选镜像不再打包。二维码库继续随原许可证打包。本次页面使用本机字体栈，无新增联网字体或图片依赖。
- `docker run … tests/browser/verify.mjs` 退出 0，67 项通过。相同命令加 `--a11y` 退出 0，23 项通过，证据为 `.runtime/hallmark-redesign/final/{behavior,accessibility}/results.json`。首轮因容器无网络接口走入离线路径，随后使用内部网络重跑。修复的失败另含日期挤压、普通行高度和详情体育标签对比度。
- `node /skill/scripts/sloplint.mjs /app --genre modern-minimal --scope app --render --kept --url …` 在 Docker Chromium 运行，退出 0。比分八宽度底线 0 FAIL，三项密度规则有记录豁免，仍有 9 条提示级发现。保留 2/2 链接、61/61 字段、5/5 数据项。三处标题调整已登记。前后证据 `.runtime/hallmark-redesign/compare-{1440,390}.png` 和 `.runtime/hallmark-redesign/hallmark/.hallmark/compare.html`。
- `sh tests/browser/visual-check.sh` 退出 0，14 屏页面截图、双端账户模式截图与点击流程通过。更新基线前看过前后对照，阈值保持通道 16、变化比例 0.002。证据 `.runtime/visual-ui-891138`。
- `sh tests/browser/integration-check.sh` 退出 0，临时 PostgreSQL 和真实 API 验证 26 个场景。替身执行核验 4、刷新 1、回执 1、交易调用 0，证据 `.runtime/web-integration-868283`。
- `bash android/build-candidate.sh` 退出 0，28 项 JVM 单测无失败，Lint、Debug、Unsigned Release 构建通过。APK 和摘要在 `android/build/artifacts/`。`adb devices -l` 未发现设备，安装、配对和离线真机验证待补。
- Docker 网页候选已构建。环回只读预览为 `http://127.0.0.1:3018/`，首页及公开赛事读取探针均成功。正式服务仍通过 3003 提供。本轮将网页候选交付为本机预览，下一步为正式发布及 Android 安装验收。
- 实现提交 `12fb20eff64506fec71f135670cc9774cd7b8bb0` 已推送到 `fix/TASK-78-hermes-telegram-sync`。推送及 `git ls-remote` 核验均退出 0，远端引用返回同一提交。开始实现前分支推进到 `06341a5d`，重构提交基于该最新版本。

状态为设计已记录。`git status --short --branch`、`git ls-remote origin refs/heads/fix/TASK-78-hermes-telegram-sync`、`docker ps` 均退出 0。安装器从 Hallmark 固定提交 `04977e15f3dfd6ee4b74c887dac3eb52665d6c92` 安装至 `/root/.agents/skills/hallmark`，退出 0。Docker 中 `theme.mjs deal --genre modern-minimal` 退出 0，三项为 Wayfind、Console 和 Dose。采用 Wayfind 的冷灰、蓝黑和钴蓝，沿用球讯中文名称和紧凑阅读节奏，以数据行和对阵本身作为视觉内容。下一步保存重构前截图与交互清单，再实现各页面。
