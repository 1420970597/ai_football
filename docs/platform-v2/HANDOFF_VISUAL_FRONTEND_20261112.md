# 网页视觉重构交接文档（2026-11-12）

本文交接 `refactor/TASK-78-web-ux` 分支上的前端视觉重构。写作原则：只写可复核的事实，实测数字附命令，推断标注「推断」并给出推翻它的检查方法。

> 2026-11-12 续接更新：第 3 节缓存缺陷已由源码 `26ac5f8c` 修复并仅切 web 上线，开发镜像 `sha256:2fb58c69…`。整组资源现在使用内容哈希路径；新 CSS 公网首次 MISS，HTML 与 9 个运行时资源逐字节核对一致，真实只读 10 个页面/视口组合通过。当前事实和局限以 `ASSET_VERSIONING_20261112.md`、`reports/web-asset-versioning-20261112.json` 为准；第 3 节保留为修复前的故障与方案历史。

接手前请先读本文件第 9 节（未解决缺陷）和第 10 节（必须保住的验收底线）。第 3 节是本次唯一发现的、会影响线上用户的新缺陷。

---

## 一、一句话现状

前端视觉重构已完成并上线；缓存缺陷续接修复后，线上 `https://football.5545.eu.cc` 网页构建来源为 `26ac5f8c`，视觉源码保持 `f0dd1c8c`。**原第 3 节的旧 CSS 混用缺陷已验证修复**；个人中心布局取向及分支合并仍待处理，分支尚未合入 `main`。

---

## 二、代码位置与提交

| 项 | 值 |
| --- | --- |
| 工作树 | `/root/ai_football/.runtime/refactor-wt/web-ux` |
| 分支 | `refactor/TASK-78-web-ux` |
| 远端 | `origin`（`github.com:1420970597/ai_football.git`），已推送且引用一致 |
| 设计依据 | `docs/platform-v2/DESIGN_BRIEF_VISUAL_20261112.md`（`96168739`） |
| 上线记录 | `docs/platform-v2/VISUAL_REDESIGN_RELEASE_20261112.md` |
| 进度台账 | `docs/platform-v2/PROGRESS.md`，`### 2026-11-12 06:50 UTC 视觉重构上线` |
| 本次封存 | `docs/prototypes/WEB_VERIFICATION_SNAPSHOT.json` |

重构相关提交（自 `f66e6500` 基线刷新起）：

```
40dc1c4f docs: pin the sealed evidence to the deployed web commit and correct the cache claim
be8d8a33 test(browser): refresh sealed evidence and the counts the sealer asserts
61e008fb docs: correct the accessibility figure to the measured 30 scans / 23 checks
59ee5901 docs: record the visual redesign release, its evidence and what is left
f0dd1c8c fix(web): collapse the recommendation card void and unclip the date strip
69d89d73 fix(web): 比分列统一 96px，删掉两处死规则并让基线看见篮球
e1fe1598 fix(web): 让比分列放得下三对三，并让断言能量出溢出
96168739 docs(web): record the visual redesign brief
fc3326f6 style(web): 把间距收敛到 4 的倍数
d3137358 feat(web): 重做专家、会员、个人中心与手机端
ad61ab18 feat(web): 重排比分页并重做专家推荐卡
7ec4870d feat(web): 建立视觉令牌与三段外壳
```

`f66e6500..f0dd1c8c` 之间 `web/app` 累计：`app.css` 3231 行变更（+2128/−1194，净到 3361 行），`app.js` 91 行变更（2269 行）。**`web/app` 自 `f0dd1c8c` 起再无改动**（`git diff f0dd1c8c -- web/app` 为空），所以线上那份源码就等于 `f0dd1c8c`。

---

## 三、已复现的缺陷：边缘缓存 + 资源无内容指纹（最高优先，未修）

### 3.1 事实

三层缓存行为不一致，这是缺陷的根源：

| 层 | `/assets/app.css` 的 `Cache-Control` | 备注 |
| --- | --- | --- |
| 容器内 nginx（`web/app/nginx.conf:73-77`） | `public, max-age=300` | 源码设定 |
| 宿主 nginx（`/etc/nginx/sites-available/football.5545.eu.cc.conf`） | 不改写，纯反代到 `127.0.0.1:3003` | |
| Cloudflare 边缘 | `public, max-age=14400` | **覆盖了源站值** |

同时 `/` 返回 `Cache-Control: no-cache`、`cf-cache-status: DYNAMIC`，即 HTML 每次都回源。于是部署后的可达状态是：**新 HTML + 旧 CSS/JS，最长 4 小时**。

实测命令与输出：

```
$ curl -sSI https://football.5545.eu.cc/assets/app.css | grep -i cache-control
cache-control: public, max-age=14400

$ docker exec ai-football-v2-web-1 wget -qS --spider http://127.0.0.1/assets/app.css
  Cache-Control: public, max-age=300     # 源站是 300，不是 14400
```

`ETag`/`304` 本身是好用的，不要误判成缺陷：

```
$ curl -sSI https://football.5545.eu.cc/assets/app.css | grep -i etag
etag: W/"6af55c9d-1198f"

$ curl -o /dev/null -w '%{http_code} %{size_download}' \
    -H 'If-None-Match: W/"6af55c9d-1198f"' https://football.5545.eu.cc/assets/app.css
304 0
```

即：协商缓存没问题，缺的是**文件名不含内容哈希**，导致边缘无法区分新旧版本。

### 3.2 后果已实测，不是理论风险

用 `f0dd1c8c` 之前的 CSS（`69d89d73:web/app/app.css`）配当前 HTML/JS，在容器内 Playwright 实测（探测脚本 `.runtime/mixprobe/probe.mjs`，源目录 `.runtime/mixprobe/old/`）：

| 视口 | 当前源码 | 旧 CSS 混用 | 差异 |
| --- | --- | --- | --- |
| 1440 推荐卡高 | 89.5px | 81.5px | −8px，正文下方空洞回来了 |
| 390 推荐卡高 | 198.4px | 190.4px | −8px，同上 |
| `.recommendation-body` `flex-direction` | `column` | `row` | 结构退回 |

不会白屏、不会 JS 报错（`errors` 只有游客 `/auth/me` 的 401）。也就是说：**用户看到的不是坏页面，而是这次刚修好的间距被悄悄退回**——这类问题不会被任何监控发现，只能靠人眼或像素比对。

### 3.3 修法（尚未实施，方案已定但未验证）

原则：所有资源 URL 带一个由内容算出的版本号，边缘缓存键随之变化，旧对象自然不被引用。**不要做逐文件哈希**——逐文件哈希必须沿模块图级联（`app.js` 内嵌 `ui.js` 的哈希，`ui.js` 又内嵌……），漏掉任一条边就会留下半旧半新的模块组合。整组共约 250KB，部署时全量重取比级联出错便宜。

具体要改四处引用点（已核实，别漏）：

| 位置 | 内容 |
| --- | --- |
| `web/app/index.html:12` | `<link rel="stylesheet" href="/assets/app.css" />` |
| `web/app/index.html:1015` | `<script type="module" src="/assets/app.js"></script>` |
| `web/app/app.css:712` | `url("/assets/cc-reference/home_announcement_icon.png")` |
| `web/app/admin.js:30` | `script.src = "/assets/qrcode.js"` |
| `web/app/app.js:365` | `slot.src = "/assets/cc-reference/home_match_vs_icon.png"` |

外加模块导入说明符（6 处，全部是相对路径 `./x.js`）：`app.js` 导入 `./ui.js`、`./identity.js`、`./account-history.js`、动态 `import("./admin.js")`；`account-history.js`、`admin.js`、`identity.js` 各导入 `./ui.js`。

我当时写了一个 `web/app/version-assets.sh`（构建期 sed 打版本号），**因为没跑过任何测试就删掉了，没有进仓库**。重做时要么走这个构建期改写路线，要么改成在 nginx 里用 `map` 把无 query 的请求重定向，必须选一条并实际验证：连续两次部署应让边缘 `cf-cache-status` 变 `MISS`，且 14 屏视觉比对仍通过。

**验证这个缺陷是否已修的唯一可靠方法**：部署新版本后立刻 `curl -sSI https://football.5545.eu.cc/assets/app.css | grep -iE 'cf-cache-status|cache-control'`，若 `cache-control` 的 URL 变了（带 `?v=`）且 `cf-cache-status: MISS`，说明新对象生效。

### 3.4 顺带需要注意

边缘把 `/assets/` 固定成 4 小时，也意味着**任何**紧急的 CSS 修复都要等 4 小时或手动清缓存。仓库里没有任何 Cloudflare 清理工具（`grep -rniE "cloudflare|purge"` 在非 `.runtime` 目录无命中），也没有 CF API 凭据落在 `.runtime/*.env` 里。若要走手动清缓存路线，需要所有者提供凭据，这属于只有所有者能做的决定。

---

## 四、部署状态（线上事实）

| 项 | 值 |
| --- | --- |
| 域名 | `https://football.5545.eu.cc`（Cloudflare → 宿主 nginx → `127.0.0.1:3003`） |
| 镜像 | `sha256:270af6e3d3d9c0b3124f738186ae77575109921888e386459cc283aae16718e2`，tag `ai-football-web:2.0.0-dev-t78ux-f0dd1c8c` |
| 容器 | `ai-football-v2-web-1`，ID `bf3b065a9565…`，`healthy`，`RestartCount 0` |
| 部署前 | 镜像 `sha256:0c39e031…`，容器 `e7884ecd…`，CSS 2496 行 |
| 线上文件 | `app.css` 3361 行 / 72079B，`app.js` 2269 行 / 115284B |
| 与工作树一致 | `sha256(app.css)=f6046ee4…`、`sha256(app.js)=139a64fd…`，容器内与工作树相同 |

候选文件（只改 `services.web.image`）：

```
/root/ai_football/.runtime/task78-visual-ux-20261112/candidate.yml
```

其余 9 个服务容器未重建（现状：`api`、`collector`、`executor`、`hermes`、`hermes-mcp`、`local-mcp-api`、`minio`、`raw-retention`、`review-worker`，加 `web` 共 10 个 `com.docker.compose.project=ai-football-v2` 容器）。

**部署命令的真实参数**（从运行中容器 label 反查，不是记忆）：

- project name `ai-football-v2`，project directory `/root/ai_football`
- `com.docker.compose.project.config_files` 共 177 条原始条目、**165 条去重**（163 个 `.yml` + 2 个 `.json`），其中 35 个是历史 `candidate.yml`。整条链是累加出来的，不要试图手写。
- `com.docker.compose.project.environment_file` 共 5 份，路径均为 `.runtime/` 下（敏感，勿复制内容）：
  `.runtime/runtime.env`、`.runtime/db/runtime.env`、`.runtime/remote/local.env`、`.runtime/minio-20261111/local-storage.env`、`.runtime/nov11-local-db/runtime.env`

重建 web 的公式（读 label、拼链、加候选、只重建 web）：

```sh
docker compose --project-name ai-football-v2 --project-directory /root/ai_football \
  $(for e in <5 个 env-file>; do printf -- '--env-file %s ' "$e"; done) \
  $(docker inspect ai-football-v2-web-1 \
      --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' \
    | tr ',' '\n' | grep -v '^$' | sed 's/^/-f /' | tr '\n' ' ') \
  -f <本次 candidate.yml> \
  up -d --no-deps --no-build --pull never --force-recreate --wait --wait-timeout 120 web
```

注意 `.runtime` 在 `.gitignore` 里（`.gitignore:19`），这些 env 文件不进 Git，也不该进。

---

## 五、验证证据（本轮全部重跑，不再引用旧文件）

全部在 Docker 内执行，宿主机只做 Docker/Git/文本。宿主机预算约束：构建 `--memory 768m --cpu-period 100000 --cpu-quota 50000`，运行 `--cpus .25–.75`、`--memory 256m–1g`。

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 主浏览器套件 | `sh tests/browser/check.sh` | **68 PASS / 0 FAIL / 0 浏览器错误** |
| 真实 API 联调 | `sh tests/browser/integration-check.sh` | 基础段 **17 PASS**（`integration-results.json`）+ 扩展段 **9 PASS**（`integration-account-results.json`），两段无重名，共 26 个场景 |
| 无障碍 | `sh tests/browser/accessibility-check.sh` | **30 扫描 / 23 检查 / 0 违规 / 0 浏览器错误** |
| 离线报告 | 见 `tests/browser/README.md` | 3 项，`REPORT.html` 25 个 `<img>` 全解码 |
| 视觉比对 | `sh tests/browser/visual-check.sh` | 像素阈值自检 + 14 屏 + 盈利专家控制组，三项 PASS |
| 封存校验 | `tests/browser/snapshot.py` | `{"checks":[68,17,3,23],"images":37}` |

封存记录（`docs/prototypes/WEB_VERIFICATION_SNAPSHOT.json`）：

- `recorded_at` = `2026-11-12T07:19:27Z`
- `web_source_commit` = `f0dd1c8c7be27d58bf6acf41bf1f6865d2baa85e`（本轮才填上，之前是 `null`）
- 四项检查分别 68 / 17 / 3 / 23，37 张图片

### 5.1 三条必须知道的执行陷阱

1. **`visual-check.sh` 必须在宿主机跑**，它内部要调 `docker`；在容器里跑会 `docker: not found`。而且要 `sh tests/browser/visual-check.sh`，直接执行会 `Permission denied`（无执行位）。
2. **`accessibility.mjs` 没有独立入口**，只导出 `runAudit`。单独运行它退出码 0 但零输出——会被误当成"通过"。必须走 `verify.mjs --a11y` 或 `accessibility-check.sh`。
3. **`snapshot.py` 的期望条数是硬编码的**，场景增删时必须同步改，否则封存器直接 `AssertionError` 而不是报警。本轮已按实跑修正并写明原因（`25→68`、`11→17`、`29→23`、`scans 38→30`）。原状态是长期跑不通的——见第 8 节。

### 5.2 无障碍数字从 38/29 变 30/23 不是回归

上线记录一度写「38 扫描、29 检查」，那是错的：旧 `accessibility-results.json`（`generated_at 2026-10-26T12:58:29Z`）被当成了本轮输出。差别来自 `d1961418`（2026-10-30，按 AGENTS.md 七.4 移除平台二次验证门禁）：

- 删掉 10 处 MFA/换机/取消弹层扫描、8 项对应检查
- 新增 2 处 `admin-system` 扫描、2 项「系统设置不提供二次验证入口」检查

当前脚本 8 处 `await scan(`、14 处 `await check(`，展开正好 30/23。已修正文档并在 `61e008fb` 说明。

---

## 六、视觉基线与像素比对

14 屏 = 7 个视图 × 1440/390：`scores`、`schedule`、`experts`、`membership`、`account-guest`、`admin-guest`、`match-detail`。

manifest 固定项（`tests/browser/visual-baselines/manifest.json`）：`browser_version 141.0.7390.37`、`locale zh-CN`、`timezone Asia/Shanghai`、`instant 2026-10-26T11:11:00Z`、`channel_threshold 16`、`max_changed_ratio 0.002`、`synthetic_data true`、`production_evidence false`、`fixture_sha256 59c897f5…`。

约束（设计简述 §6 的硬规则，不要绕过）：基线只能从真实渲染重新生成，14 屏必须逐张人工复核，基线与被测 CSS 必须同一提交；`--update` 需要环境变量 `VISUAL_BASELINE_UPDATE=reviewed`，否则 `visual-regression.mjs:11` 直接抛错。

控制组 `profitable-expert-baselines` 也更新了：`profitable-mode-1440.png` 从 797×100 变为 385×137。原因是 `#account-content` 改两列网格后控件在列内重排，不是回归；控制组断言（点击行为、本人确认、限额保留、手动回滚）全部继续通过。

**确定性已实测**：同源连续两次生成，在 `channel_threshold:16` 下 14 屏差异像素均为 0。换网络环境仍可能有阈值以下抗锯齿差异，若出现亚阈值差异应先量 `changed_pixels` 再判断是否为真回归。

---

## 七、本次修掉的三处排版缺陷

均先测量、后修改，数字可复现（探测脚本保留在 `.runtime/{cardfinal,cardab,dateprobe}/`）：

| 缺陷 | 证据 | 修法 | 结果 |
| --- | --- | --- | --- |
| 推荐卡正文下 31.9px 空洞 | 1440/1030 卡高 113.4px，元信息 `grid-column:2/-1` 自成一行，卡高由 44px 按钮决定 | 元信息并入 `.recommendation-body` flex 列 + `margin-top:auto`（`app.css:1048`、`:1086`；`app.js` 调整闭合位置） | 卡高 89.5px，赔率→元信息 0，元信息→卡底 9px |
| 窄屏卡多 8px | 390 卡高 206.4px，`priceToMeta` 8px | 窄屏 `margin-top:0`，间距由 `padding-top` 提供（`app.css:3208`） | 198.4px，与桌面口径一致 |
| 筛选栏折两行 | `#league-filter` 包在 `<label>` 内，`.filter-row > .small-button` 子选择器够不到 | 改后代选择器 `.strip-filters .small-button` + `white-space:nowrap; flex:0 0 auto`（`app.css:2722`） | 46px → 26.5px |
| 选中日期被裁 | 390 裁 22px、360 裁 52px，`scrollLeft:0` | 只对 `#date-buttons` 算 `scrollLeft` 居中（`app.js:249`） | 390 下 `scrollLeft:79` |

日期条那条有个坑值得记住：**不能用 `scrollIntoView`**。它会连带滚动整个文档，把 Chromium 的 Tab 顺序起点挪到当前视口，导致第一个 Tab 跳过页首的「跳至主要内容」——无障碍会因此失败。

---

## 八、本轮修掉的验证基础设施问题

`tests/browser/snapshot.py` 的封存校验长期跑不通，不只是「数字过时」：

- `integration-results.json` 期望 11 行，实际 17 行（`4ec64643` 补齐账户浏览器成功链时新增 6 场景）
- `accessibility-results.json` 期望 29 检查，实际 23；`scans == 38` 断言对应不上实际 30
- `results.json` 期望 25 行，实际 68 行（`2550e106` 之后持续扩充，`d1961418` 删 3 项换机场景）

三个输入文件也全是旧的（`accessibility` 停在 10-26、`browser` 停在 10-26、`integration` 停在 10-27）。已在 `be8d8a33` 重跑并修正，注释里写明是哪次提交改变了场景集合。**以后增删场景必须同步改这几行**，否则封存器失效。

顺带确认：`report-results.json` 没漂移。`REPORT.html` 仍是 25 个 `<img>`，与记录一致；`images/` 目录下 37 个文件里多出的是源 SVG/PNG，不参与页面加载。

---

## 九、未解决事项（按优先级）

### 9.1 边缘缓存 + 资源无指纹 —— 已修复并上线

续接验收见 `ASSET_VERSIONING_20261112.md`：真实 nginx 两代产物/保留旧缓存的浏览器模拟通过；原 14 屏基线未更新且全部通过，买入按钮 18 项、主浏览器 68 项、无障碍 30 扫描/23 检查通过；公网 HTML/9 个运行时资源与镜像一致，10 个游客只读页面/视口组合通过，其他 9 个同项目容器未变。镜像只保留当前版本目录，已打开旧页面第一次懒加载旧模块时仍可能需要刷新；不宣称完成跨版本资源留存。

### 9.2 个人中心 1440 两列行高不均（需要所有者拍板）

`#account-content` 是 `grid-template-columns: repeat(auto-fit, minmax(360px, 1fr))`（`app.css:1858`），1440 下两列。`我的乐鱼账户`（`index.html:349`）高 465px，`自动投注与限额`（`index.html:397`）高 816px 同排，前者下方空约 351px。

测过三种方案，都能改善空档但各有代价，**没纳入上线**：

1. `column-count` 多列流：会改变分区上下顺序
2. 整行跨列：两列布局失效，等于单列
3. 单列全宽：滚动变长

这是产品视觉取向问题，不是技术选择，交给所有者定。要复现就量这两个 `<section>` 的 `getBoundingClientRect().height`。

### 9.3 分支未合并

- `refactor/TASK-78-web-ux` 领先 `origin/main` **1351 个提交**，落后 **0 个**，`origin/main` 是它的祖先（可 fast-forward）。
- 这 1351 个提交里 **57 个动 `web/app`**、**309 个动 `services/`**——也就是说这条分支远不止前端重构，还带着后端采集、审核、存储的改造。合并前要有人明确这些后端改动是否都要进主线。
- `origin/main` 最后一次提交是 2026-10-27，已落后很多。

### 9.4 主检出带他人的未提交改动（绝对不要动）

`/root/ai_football` 在 `fix/TASK-78-hermes-telegram-sync` 分支，含他人未提交改动：

```
 M services/api/app/account_jobs.py
 M services/executor/account_protocol.py
 M services/executor/worker.py
 M tests/test_account_protocol.py
?? .omc/  .pi/  docs/platform-v2/REVIEW_RESULT_SPORT_STORAGE_20261112.md
?? reports/feijipan-share-20261031/
```

本次上线的前端源码来自工作树分支，与这些改动无关。合并时统一，不要 revert / stash / 暂存这些文件。也**禁止裸 `git stash`**——栈在工作树之间共享。

### 9.5 磁盘

`/` 已用 205G/251G（85%），`/root/ai_football/.runtime` 占 29G，`reports/feijipan-share-20261031/` 占 4.1G，Docker 有 137 个 `ai-football` 镜像、457 个卷（30 个活跃）。

**不得删除**：生产 PG 卷 `ai-football-v2-local-pgdata`、备份、`ai-football-e*-restore-data` 四个卷（`e311/e343/e351/e2212`，需先做保留研究 + 清理方案入库，AGENTS.md 第二节第 5 条）、其他项目的容器与卷。遗留容器 `ai-football-v2-pgverify` 已运行两周，属待清理评估对象，不是本次可动。

### 9.6 其他遗留（非本次范围）

- `S3_SECRET_KEY` 仍需轮换（历史暴露，见 `HANDOFF_UNRESOLVED_20261112.md` §4.5）
- Android 无当前候选包
- 免费套餐携带 `automatic_betting` 权益（真实资金准入面，只有所有者能定，见 `HANDOFF_UNRESOLVED_20261112.md` §1.1）
- Telegram 409、`unsupported` 媒体、约 2045 条历史 `unresolved` 审核任务等见 `HANDOFF_UNRESOLVED_20261112.md`

---

## 十、必须保住的验收底线

改前端时这些断言就是合同，不要为了让新设计通过而放宽：

1. `verify.mjs` 冻结的「1440 视口下 `.match-row` 高度 ≤ 45px、联赛列宽 ≥ 110px」——密度是产品价值，视觉新意不能靠撑高行来换。
2. 14 屏视觉基线比对必须通过；基线只能从真实渲染重生成并逐张人工复核。
3. 无障碍 0 违规；`skip` 链接必须是首个 Tab 落点（`scrollIntoView` 会破坏它）。
4. 关键数据用十进制，时间统一 UTC 入库、界面显式转换；不得吞异常后继续显示健康（AGENTS.md 十三.2）。
5. 任何前端改动都不得绕过后端身份、权益、归属与写操作校验（AGENTS.md 七.2）——按钮隐藏不算校验。
6. 实盘验收默认用协议样本与无资金测试；未经所有者针对真实交易的明确授权，不得实盘下注来证明功能。

---

## 十一、常用命令速查

```sh
cd /root/ai_football/.runtime/refactor-wt/web-ux      # 工作树

# 浏览器主套件（宿主执行）
sh tests/browser/check.sh
# 真实 API 联调（起临时 compose 项目，含扩展段）
sh tests/browser/integration-check.sh
# 无障碍（宿主执行，内部走 verify.mjs --a11y）
sh tests/browser/accessibility-check.sh
# 视觉比对（宿主执行；更新基线需 VISUAL_BASELINE_UPDATE=reviewed）
sh tests/browser/visual-check.sh
# 封存校验（无输出即失败；成功打印 JSON）
VERIFICATION_SOURCE_COMMIT=$(git rev-parse HEAD) python3 -I tests/browser/snapshot.py

# 线上事实核对
curl -sSI https://football.5545.eu.cc/assets/app.css | grep -iE 'cache-control|etag|cf-cache-status'
docker inspect ai-football-v2-web-1 --format '{{.Image}} {{.State.Health.Status}} {{.RestartCount}}'
docker exec ai-football-v2-web-1 sha256sum /usr/share/nginx/html/assets/app.css
sha256sum web/app/app.css      # 两者应相同
```

宿主机只做 Git/Docker/归档/文本/权限；应用、构建、测试、数据库全部在 Docker 内，并带 CPU/内存限制。

---

## 十二、给下一位的一句话

第 3 节缓存修复已经完成，先核对续接证据，再按需要处理个人中心两列布局取向和分支合并；**合并前确认那 309 个后端提交是否都该进主线**。效率优先，复用现有通过证据，只有新增改动、失败或未解疑点才扩大或重跑验证。
