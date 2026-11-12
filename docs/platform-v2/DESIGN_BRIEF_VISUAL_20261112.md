# 网页视觉重构设计简述（2026-11-12）

设计由 Claude 完成，实现委托子代理执行。目标目录为工作树 `refactor/TASK-78-web-ux` 的 `web/app/`。
本文件是实现依据，不是愿望清单：任何与本文件冲突的「更好看」想法都按本文件执行。

## 零、为什么改，改到什么程度

现有页面不是没有样式，而是**没有层级**：11–12px 的文字贯穿全站，主队、比分、联赛、盘口、按钮、说明共用同一档字号与同一档灰度，于是读者找不到第一眼该看的地方；同时若干处渲染出**逐行重复的空话**，把留白用噪声填满。

`#scores?scope=recommended` 在真实基线上每行都跟着一句「关联记录已变更，请刷新列表。」，盘口列每行都是「暂无专家盘口」；专家页只有 1 行数据，下方 400px 是纯白。这些不是审美问题，是信息设计问题。

目标：**焕然一新，但一屏能看到的比赛数量不减少**。这是一个实时比分站，密度本身就是产品价值。因此：

> **硬性取舍（本简述的核心决定）**：`verify.mjs` 冻结的「1440 视口下 `.match-row` 高度 ≤ 45px、联赛列宽 ≥ 110px」**保持不变**，CSS 与断言都不动。视觉新意必须来自字号层级、区块间距、卡片做工、状态表达与移动端重排，而不是把行撑高。把行撑到 60px 让一屏少两场比赛，是退步。

## 一、视觉概念：「赛场数据台」

不是通用后台仪表盘，也不是电商卡片墙。参照的是比分站的行业惯例：**深色报头 + 亮底数据表 + 极细分隔线 + 只用颜色表达状态**。

- 报头承担品牌与导航，深藏青，不做渐变装饰。
- 内容区是「纸」，白底，靠 1px 分隔线和 4px 网格的留白分层，靠色块只在必要处（状态、可执行动作）。
- 颜色只有三种语义：红=进行中/胜/涨，绿=负/跌，橙=真实资金动作。品牌蓝只用于链接与选中态。**不得新增第四种强调色。**
- 数字优先：比分、赔率、收益、准确率一律 tabular-nums，且比同排文字大一档。

## 二、令牌（在现有 `:root` 上扩展，不推翻）

保留全部现有令牌名与值，只做增量。允许微调的两个值已标注。

```css
/* 已有，保持 */
--header:#0e2440; --on-dark-muted:#b8c7da; --brand:#0b5fc4; --brand-strong:#084c9e;
--brand-soft:#e9f1fc; --focus:#0a3f8f; --ink:#17212e; --ink-2:#3b4858; --muted:#586576;
--line:#dfe5ec; --line-strong:#7a8797; --paper:#fff; --surface:#f6f8fa; --surface-2:#edf1f5;
--background:#eef2f6;
--red:#c4291c; --red-soft:#fdeeec; --red-line:#f3c9c4; --green:#11693f; --green-soft:#e6f5ec;
--warn:#8a5200; --warn-soft:#fff5df; --warn-line:#f0d9a8;
--buy:#c2410c; --buy-strong:#a8370a; --orange:#f08a24; --orange-text:#a8440a; --gold:#8a6420;
--radius:6px; --radius-lg:10px;
--radius-pill:999px;
--shadow:0 1px 2px rgb(14 36 64/.06), 0 6px 20px rgb(14 36 64/.07);
```

新增：

```css
/* 报头层次：hover/次级面板用，不改变主色身份 */
--header-2:#132c4d;
/* 三级文字，介于 --ink-2 与 --line-strong 之间，仅用于元信息 */
--ink-3:#6b7889;
/* 字号阶梯。正文仍 12px，这是密度基线，不是遗漏 */
--text-2xs:10px; --text-xs:11px; --text-sm:12px; --text-md:13px;
--text-lg:15px; --text-xl:20px; --text-2xl:26px;
/* 间距阶梯：全站只允许 4 的倍数 */
--s1:4px; --s2:8px; --s3:12px; --s4:16px; --s5:24px; --s6:32px;
/* 行高低位与呼吸 */
--row-h:36px;
--ring:0 0 0 3px rgb(11 95 196/.28);
```

对比度要求：所有新增前景/背景组合 ≥ 4.5:1（≥18.66px 粗体或 ≥24px 可放宽到 3:1）。现有 `--muted` 在白底为 5.2:1，可直接用；`--ink-3` 在白底为 4.6:1，只允许用于 12px 以上文字。

## 三、结构：桌面三段、手机单列

断点保持现状，不新增：`≥1200`、`1191–1030`、`721–1030`、`≤720`、`≤640`。

- **桌面**：`main-layout` 三段。比分页在 ≥721px 下隐藏左栏（现状如此，保留）→ `内容 + 220px 右栏`。
- **移动**：隐藏左右栏与顶部主导航，改用 `nav.mobile-bottom` 五项。底部导航图标把现有字符（▣ ◎ ♧ ♙）换成内联 SVG（24×24，`currentColor`，`stroke-width:1.6`），不引入字体或图片。

各页在同一外壳内，不允许为某个页面另立一套报头。

## 四、逐组件设计

### 4.1 报头与工具条

- 工具条 30px，12px `--muted`，`--surface` 底；左「球讯比分 · 赛事与专家记录」，右时间/管理入口/会员中心。
- 报头 56px（≥721px），`--header` 实色，底部不留阴影；滚动时 `position:sticky` 保留。
- 品牌：`--orange` 34px 圆角方块 + 20px/700 名称 + 9px 字距 2px 的副标。副标颜色 `--on-dark-muted`。
- 主导航：14px，未选中 `--on-dark-muted`，hover 变白；选中白色 + 3px `--orange` 下划线（现状保留，仅微调下划线圆角为 2px）。
- 报头右侧 `#account-entry`：8% 白底、35% 白边，hover 16%；移动端改为纯文字按钮，去掉边框。
- 新增：报头内「滚球 N 场」实时计数徽标，仅当 `state.matches` 中存在 `phase === 'live'` 时显示，`--red` 圆点 + 12px 白字。不发起新请求，只读已有 state。

### 4.2 比分页骨架

- `.view`：白底，1px `--line`，**去掉 10px 大圆角**，桌面端只保留 `--radius`。主内容列与右栏之间靠 12px 间距分隔，不靠双层边框。
- `.section-title`：56px 高，`h1` 20px/700 `--ink`；右侧 `#last-updated` 做成 `.meta-chip`（11px、`--surface-2` 底、圆角 pill、左侧 6px 圆点，数据新鲜时 `--green`，过期时 `--warn`）。底部 2px `--brand` 边框改为 1px `--line` + 选中区块 `--brand` 左侧 3px 竖条，避免整页出现两条粗蓝线。
- `.sports-bar`：改为分段控件——`--surface-2` 轨道、内边距 3px、圆角 pill；激活项白底 + `--shadow` + `--brand` 文字；未激活 `--ink-2`。右侧 `#sport-context` 11px `--muted`。
- `.scores-toolbar`：scope 标签（专家关联/全部赛事）用小号胶囊，激活为 `--brand-soft` 底 + `--brand` 字；状态标签（全部/滚球/未开赛/已完场/取消）用下划线 tab，激活 `--brand` 2px 下划线。
- `.date-strip`：单行、`--surface` 底、`#date-buttons` 每格等宽，格内上行 12px 日期、下行 10px 星期；激活格 `--brand` 实底白字；今天未激活时加 `--brand` 描边。移动端改为横向滚动 + `scroll-snap-type:x mandatory`，不换行。
- 表头 `.score-table-head`：11px、`--muted`、`--surface-2`、**sticky，`top` 等于报头高度**、`z-index:5`、下边框 1px `--line-strong`。
- 行 `.match-row`：白/`--surface` 交替（现有），hover `--brand-soft`，`transition:background-color 120ms`。`[data-phase="live"]` 左侧 3px `--red` 竖条（用 `::before` 绝对定位，不占据网格列，不改变行高）。
- 联赛列：11px `--muted`，单行省略号（现状 `max-width:100px`），`title` 保留全称。收藏星：18px，未选 `--line-strong`，选中 `--orange-text`。
- 时间 11px `--muted`，状态 11px（live 用 `--red` 且加 600 字重）。
- 主客队 13px `--ink`，`team-home` 右对齐 / `team-away` 左对齐。
- 比分 `.score-button`：**20px/700** tabular-nums，常规 `--ink`，live 用 `--red`（保留现有 `.live + .team-home + .score-button` 选择器逻辑）；未开赛 VS 图标 24×18 保持。
- 盘口列 `.match-odds`：筹码式——`--green-soft` 底、`--green` 字、2px 圆角、10px 内边距 1×3；市场名单独一行 9px `--muted` 无底色。`.stale` 用 `--surface-2` + `--muted`。
- 详情按钮：11px `--brand`，文案必须是 `详情`（测试断言），宽 40px 列。

### 4.3 推荐区（产品差异点，本次重点）

当前 `.match-recommendations` 是行下方一整条浅灰虚线区，每条推荐一行文字，行高由内容撑开，且没有信号时渲染一句空话。

新设计：

- 有信号时，行下方渲染**卡片列表**：`--surface` 底、上边框 1px `--line`、内边距 8px 12px；每张卡白底、1px `--line`、`--radius`、4px 8px 内边距、卡片间距 6px。
- 卡片网格：`[头像/昵称] [盘口描述 + 赔率] [状态徽标] [买入按钮]`，`grid-template-columns: minmax(96px,auto) minmax(0,1fr) auto auto`，`align-items:center`，`gap:8px`。
- 专家：24px 圆形头像（首字，`--brand-soft` 底 `--brand` 字）+ 12px/600 昵称，纵向排列。
- 盘口：单行，`盘口 · 期间 · 盘口线 · 选择`，12px `--ink-2`，选择项加粗 `--ink`；赔率单独 `15px/700` tabular-nums `--ink`。赔率前的标签文案**必须仍是「推荐时系统赔率」**（`homepage-expert-matches.mjs` 断言 `/推荐时系统赔率 1.95/`），只允许把它做小到 10px `--muted`，不得改写文字、不得在标签与数值之间插入其他字符。
- 状态徽标：pill，`--radius-pill`，10px，padding 2px 7px；`pending` 灰、`win/half_win` `--red-soft`/`--red`、`loss/half_loss` `--green-soft`/`--green`、`push/void` 灰。
- 元信息行：`阶段 · 报价时间` 10px `--muted`，`原文与审核` 链接 11px `--brand`。
- 买入按钮 `.manual-bet`：保持 `--buy` 实底白字、`min-height:44px`、`padding:0 18px`、`font-weight:600`、`white-space:nowrap`；hover `--buy-strong`；`active` translateY(1px)；`:focus-visible` 用 `--ring`。禁用态保持 `--surface-2` 底 + `--muted` 字（对比度 5.2:1，达标），**且必须保留 `aria-describedby` 指向的原因文字**，原因 11px `--muted`，紧跟按钮下方，最多两行。
- `.manual-bet-result` 结果条：保留现有三种 tone（默认/pending/failed）的左边 3px 竖条语义，改为 6px 圆角浅底条，12px。
- **无信号的比赛：整段不渲染。** `matchRecommendationMarkup` 在 `signals.length === 0 && !expert_signals_has_more` 时返回空字符串，删除「关联记录已变更，请刷新列表。」。盘口列对应渲染 `<span class="odds-none" aria-label="本场暂无专家盘口">—</span>`（`--line-strong`，10px 无底色），替换「暂无专家盘口」。
- `.manual-bet-panel`：紧凑设置条——三个数字输入（每单金额/每日限额/开放敞口）等宽、右对齐 tabular-nums、单位「元」作为后缀 11px `--muted`；保存按钮 `--brand` 描边；`#manual-bet-status` 11px。整条在未登录时只显示一行提示与登录按钮。

### 4.4 右栏

三段高度接近，避免「三个空盒子」。

- `.side-panel`：白底、1px `--line`、12px 内边距、`--radius`；标题 14px/600 + 右侧「更多 ›」11px `--muted`。
- 专家榜：每项 44px，`[2 位数字名次] [28px 圆形头像] [昵称 12px + N 条已结算 10px] [收益 13px/700 右对齐]`；第 1 名名次用 `--gold`。名次数字 tabular-nums。
- 球讯会员卡：`--brand-soft` 渐变改为 `linear-gradient(135deg,#eef4fe,#e2edfd)`；18px/700 两行文案；按钮 `--brand` 实底白字 32px 高、圆角 pill、`--shadow`。
- 数据状态：三条列表，每条 `[8px 圆点] [标签 11px --muted] [值 11px --ink]`，圆点 `--green`/`--warn`/`--line-strong` 表达状态；底部 11px 说明行。

### 4.5 专家页

- 顶部 4 个统计块（专家数 / 有效结算 / 准确率 / 1单位净收益），`--surface` 底、无边框、分割线为 1px `--line`，数值 20px/700 tabular-nums，标签 11px `--muted`。数据取自现有 `state.experts` 汇总，不新增请求；不足时显示「样本不足」。
- 专家列表：表头 11px `--muted`；行 56px，`[40px 头像] [昵称 13px/600 + 副信息 10px --muted] [有效结算] [准确率] [1单位净收益]`；右侧收益 13px/700，正 `--red` 负 `--green`；行 hover `--brand-soft`；关注星按钮 32px 命中区。
- 列表容器最小高度改为 `auto`，页脚说明紧贴列表最后一行，消除基线上的大片空白。
- 筛选器（运动/统计窗口/阶段/玩法/来源/应用筛选）统一 32px 高、`--line` 边框、`--radius`，横向排列，`≤720` 时两列换行。

### 4.6 会员页

- `member-hero`：`--brand-soft` 底、无边框、24px 内边距；`h1` 26px/700；当前状态徽标 pill。
- 套餐卡：1px `--line`、`--radius-lg`、20px 内边距；价格 26px/700 + 11px 单位；权益清单 `✓` 用 `--green`，行距 1.9；CTA 全宽 40px `--brand` 实底。当前有效方案卡加 2px `--brand` 描边 + 右上角徽标。
- 支付不可用时：卡片内 11px `--warn` 一行说明，按钮禁用且文案不变。

### 4.7 个人中心与管理页

- 个人中心：卡片式分区（账户、会员、乐鱼账户、自动投注、下注金额），`--radius`、16px 内边距、标题 15px/600；≥900px 时两列网格，移动端单列。表单控件 36px 高，标签 11px `--muted` 上置。
- 管理页：保持信息密度优先，表头 11px `--surface-2`，行 44px；危险动作按钮用 `--red` 描边而非实底；`[data-admin-tab]` 用下划线 tab。

### 4.8 对话框

- 遮罩 `rgb(11 26 46 / .55)` + `backdrop-filter:blur(4px)`（无支持时退化为纯色）。
- 面板白底、`--radius-lg`、`--shadow`；内容弹窗 `max-width:760px`、确认/登录 `max-width:420px`；`max-height:min(86vh, 900px)`，内部滚动。
- 头部 sticky、标题 15px/600、关闭按钮 32px 命中区；底部动作区 sticky、右对齐，主按钮 `--brand` 实底、次按钮描边。
- 打开动画 120ms `opacity + translateY(4px)`，`prefers-reduced-motion` 下禁用（已有全局规则）。

### 4.9 移动端（≤720）

- 报头 52px：品牌 18px、右侧登录纯文字。
- 运动切换占满整行，每个选项 44px 高。
- 状态与 scope 标签合并为一行可横向滚动，不换行。
- 行重排（保留现有网格区域映射）：第一行 `时间 · 状态`，第二行 `主队 比分 客队`，第三行盘口，联赛与收藏位于左侧竖排。**盘口必须在队伍/比分按钮下方**（测试断言 `oddsTop >= buttonBottom`），且行内不得横向溢出。
- 推荐卡：单列，买入按钮整行宽、48px 高。
- 底部导航 56px + 安全区，5 项等宽，图标 24px + 10px 标签，激活 `--brand`。

## 五、不可破坏的验收下限

以下由 `tests/browser/` 既有断言固定，重构**必须全部继续通过**，且不得为绕开断言修改测试（视觉基线除外，见第六节）：

1. `verify.mjs`：16 行、12 行有 VS 图标、live 行无 VS、`.match-row` 高度 ≤ 45px、`.match-league` 宽度 ≥ 110px、`.detail-button` 文本为 `详情`、末列表头为 `详情`、无横向溢出（1440 / 390 / 360 / 720）。
2. `manual-bet.mjs`：五种不可用原因文案与 `aria-describedby` 关联、金额未设置时按钮仍可见且指向「设置」、可用时按钮高 ≥ 44px、确认框包含 `利物浦 对 曼城` / `林老师 TG` / `亚洲让球` / `1.96` / `2.00 元`、**「全场」在确认框中恰好出现一次**、`#confirm-title` 为 `确认实盘买入`、`#confirm-yes` 含 `买入`、结果条含「已预留」且 5.2 秒后仍可见、409 时显示「当前盘口已变化」且按钮恢复可用。
3. `homepage-expert-matches.mjs`：`recommended` 为默认 scope 且 `aria-pressed=true`、1 行 2 卡、含 `推荐时系统赔率 1.95`、篮球比分不得盖住队名、无横向溢出、空态文案 `暂无符合条件的专家关联比赛`。
4. `accessibility.mjs`：axe wcag2a/2aa/21aa/22aa/best-practice 零违规，且 color-contrast 的 incomplete 项按脚本自身阈值复核通过。
5. 除上述之外，`#matches-list`、`.match-row[data-fixture]`、`.match-odds`、`.match-phase`、`.team-home/.team-away/.score-button`、`[data-recommendation]`、`[data-bet-signal]`、`#confirm-*`、`#matches-state`、`#matches-notice`、`#service-state`、`[data-expert-row]`、`[data-expert-tab]`、`[data-admin-tab]`、`.mobile-bottom [data-page]` 等**全部选择器与 id 不得改名或删除**。测试看不到的东西可以重画，测试看得到的东西必须原地保留。

## 六、视觉基线

`tests/browser/visual-baselines/` 的 14 张截图（7 屏 × 1440/390）是旧设计的冻结证据。重构后必须重新生成，但**只允许由真实渲染产物重新生成**：

- 先在 Docker 内跑 `tests/browser/visual-check.sh`，接受像素比对失败，读取证据目录下的 `candidate-*.png`。
- 由 Claude 逐屏审阅这 14 张图；只有审阅通过的版本才允许用 `VISUAL_BASELINE_UPDATE=reviewed` 重写基线。
- 基线与 CSS 修改放在同一个提交里，便于回溯「哪次视觉改动对应哪版基线」。禁止先改基线再看效果。

## 七、实现纪律

1. 只改 `web/app/app.css`、必要时 `web/app/app.js`（仅渲染函数与 class 名）、`web/app/index.html`（仅必要的 class 与图标）。不动 `web/app/nginx.conf`、Dockerfile、后端、测试逻辑。
2. `app.css` 保持单文件，重排为带目录注释的分段结构（tokens / base / shell / scores / recommendations / experts / membership / account / admin / dialogs / mobile），注释用中文、只写非显然理由，不复述 CSS 本身。
3. 不引入字体、框架、图标库、图片或新网络请求。图标一律内联 SVG。已有 `/assets/cc-reference/` 两张图继续使用。
4. 所有间距为 4 的倍数；所有颜色来自令牌，禁止散落十六进制值（渐变、阴影允许 rgb() 写法）。
5. `prefers-reduced-motion` 下禁用全部动画（现有全局规则保留）。
6. 每个阶段在 Docker 内自测，不在宿主机跑任何应用代码。浏览器镜像 `ai-football-browser:v2.0.0-dev`，网络与资源限制沿用 `check.sh` 的写法（`--cpus=.25 --memory=384m --shm-size=96m --pids-limit=256`）。
7. 分阶段提交（Conventional Commits，结尾 `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`）：① 令牌与外壳 ② 比分页与推荐卡 ③ 专家/会员/个人中心/管理 ④ 移动端与对话框 ⑤ 视觉基线与截图证据。每阶段提交前跑对应功能测试。
8. 已知失败只允许是像素基线差异。任何功能断言失败视为本阶段未完成，必须修到通过，不得改断言。
