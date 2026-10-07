# 交接文档（HANDOVER）

> **项目**：`ai_football` —— 足球赛事数据采集 / 盘口经济学分析 / LLM 决策 / 本地统计
> **远程**：`git@github.com:1420970597/ai_football.git`
> **本文件所在分支**：`fix/TASK-11-stale-backfill-quotes-not-live`（基于 `main@6b9a35b`，**待 PR 合并**）
> **交接日期**：2026-10-07（初版 2026-10-06）
> **代码状态**：`a482c39` · 全量 `Ran 1115 tests … OK (skipped=11)` **exit 0** · pyright 0 error · 容器 healthy
> **本文档读法**：§1 先看"现在能不能跑"，§2~§4 是三种状态的工作事项，§6 是踩过的坑（**最省时间的一节**）
>
> ⚠️ **接手人先看这条**：本轮修的两个 bug 都有"**看起来已经修过、其实从另一条路径又长回来**"的特征
> （§6.3 → §6.5）。改代码前请先读 §6，特别是 §6.5 的**设计不变量**（陈旧行情必须返回
> `None` 而不是空集）—— 它很容易被"顺手优化"成空集，而那会让看板变空白。

---

## 0. 一句话现状

系统已从"**页面空白 + 零买入建议 + 无任何统计**"修到"**5 个视图可用、盘口变动自动触发决策、
本地台账落盘并可结算**"。核心链路已验证可跑；**唯一外部阻塞是乐鱼会话凭据**（见 §5.1）。

```text
浏览器 3001  →  5 个视图（赛事看板 / 实时盘口 / 历史战绩 / 定价对比 / 校准报告）
API    8000  →  29 个端点
采集   WS    →  盘口推送 + 比分推送 + 结束通知，落盘 _live/ 与 _trends/
决策   后台  →  盘口变动触发（30s 防抖）+ 600s 定时兜底
结算   后台  →  每 60s 回填赛果、算命中率/ROI/CLV
```

> ⚠️ 上方端口是**本机实测值**，不是默认值：`REDIS_PORT=6380`（6379 被别的项目占）、
> `CONSOLE_PORT=3001`（3000 被 new-api 占）。完整命令见 §1.1。

### 0.1 本轮（2026-10-07）做了什么

| 类别 | 内容 | 为何重要 |
| --- | --- | --- |
| **真 bug（严重）** | 陈旧回填行情冒充"进行中" | 把一个**完全停摆**的采集链路报成"527 场真实进行中"，见 §6.5 |
| **真 bug** | `sogou_searcher.py` 的 `print_articles` **定义整个丢失** | 5 处调用 / 0 处定义 ⇒ 该模块自带 CLI 一走到输出就 `AttributeError` |
| **测试基础设施** | 4 处 `inspect.getsource` 断言改为查**编译后代码对象** | 原断言会同时产生**假失败**与**假通过**，已用变异测试证明非空洞 |
| **工具链事故** | pi-lens 每回合自动 `ruff format`，**在测试跑动时改文件** | 补丁 diff 放大 5~10 倍 + 使测试出现幻影结果；已在 `.pi-lens.json` 关停（md5 实证） |
| **文档纠错** | AGENTS.md 3 处环境事实经实测证明**是错的** | 曾误记"宿主机 Python 3.12.3 / 无 pip / bs4+numpy+matplotlib+pandas 可用" |

---

## 1. 快速上手（新接手请按此顺序执行）

### 1.1 启动

```bash
cd /root/ai_football

# 端口冲突提示：6379 已被别的项目占用，本项目用 6380；
#                3000 也被别的项目（new-api）占用，故 web 控制台用 3001
REDIS_PORT=6380 SCRAPER_PORT=8081 ANALYTICS_PORT=8000 CONSOLE_PORT=3001 \
  docker compose -f docker/docker-compose.yml up -d --build analytics-api web-console
```

> ⚠️ **必须带这 4 个端口变量**，否则 redis 会与宿主机的 `nexus-mail-redis` 抢 6379 而启动失败。

### 1.2 验证是否正常

```bash
docker compose -f docker/docker-compose.yml ps          # analytics-api 应为 healthy
curl -s localhost:8000/health                            # 应为 {"status":"healthy",...}

# 采集是否活着（关键：看"最新写入"是不是几分钟内）
curl -s localhost:8000/api/v1/realtime | python3 -m json.tool | head -20
curl -s localhost:8000/api/v1/analysis | python3 -m json.tool   # 看 live.count vs upstream

open http://localhost:3001/                              # 5 个视图
```

### 1.3 跑测试与类型检查

```bash
python3 -m unittest discover -s tests -q      # 1115 passed
# ⏱ 实测耗时（本机仅 2 核，别拿旧文档的“5.5 分钟”当预期）：
#    空闲 ≈17 分钟；3 个容器同时在跑 ≈36 分钟。跑之前先确认没有重任务在抢 CPU，
#    否则会误以为“卡死了”。
/root/.pi-lens/tools/node_modules/.bin/pyright  # 0 errors
```

> 宿主机无 `pytest`，因此**统一用 `unittest`**（`python3 -m pytest` 会失败）。
> pip 虽在（实测 23.0.1），但受 **PEP 668**（externally-managed-environment）保护，
> 直装会报错 —— 要装包请用虚拟环境或容器，不要 `--break-system-packages`。
>
> ⚙️ **`.pi-lens.json`（本仓库自带）**：把 pi-lens 的 `format.enabled` 关成 `false`。
> 因为它在**每个回合结束时**自动 `ruff format` 被改动过的文件，会把补丁 diff
> 放大 5~10 倍、把仓库风格改得不一致（全仓 60 个文件里只有 12 个符合该格式），
> 并在测试运行中**静默改写文件**造成幻影结果。成因与实证见 §6.6。

---

## 2. ✅ 已完成的工作

### 2.1 采集与会话（`collector/`）

| 项 | 内容 | 关键文件 |
| --- | --- | --- |
| 盘口推送 | `C105` 周期全量快照 → `LiveBook` 内存实时表 + `TrendStore` 走势落盘 | `leyu_realtime.py` |
| 比分/结束 | `C103` 比分、`C109` 结束 → `ScoreStore` **落盘**（结算的唯一赛果来源） | `leyu_realtime.py` |
| 大响应截断 | `IncompleteRead` 显式捕获 + 限长分块读 + 完整性校验 | `leyu_client.py` |
| 会话链 | `h5-cookie → app-launch → **app-login**（新增）→ cmd/file/env` | `session.py` |
| **登录续期** | 账号口令 → `x-api-token`（**推翻"必须人工"的旧结论**，见 §6.1） | `leyu_app_login.py` |
| 会话缓存 | `_session.json` 回退（token 过期后业务会话仍可用） | `session.py` |

### 2.2 决策链路（`service/`）

| 项 | 内容 |
| --- | --- |
| 触发时机 | **盘口变动自动触发**（30s 防抖 + 20s 全局节流 + 每批 12 场）+ 600s 定时兜底 |
| 入场门控 | 经济学算法先筛（水钱/去水分歧/走势/流动性），通过后才调 LLM |
| 门控阈值 | 去水分歧阈值**随水钱缩放**（`max(3pp, 1.5×水钱)`，上限 12pp）—— 修 69/71 场误杀 |
| 快照去重 | 每个盘口只取最新一份（修"216 条历史里挑到 4.6 小时前旧价"） |
| 时效门禁 | 赔率超 600s 直接拒用（修"比分已 1:3 却建议买进球数>2.5"） |
| LLM 污染护栏 | `\|p_llm − p_market\| > 0.35` 丢弃 + 比分**不再下发**给 LLM |
| 中文标签 | 乐鱼风格（`曼联上半场-1` / `上半场进球数>1/1.5`），带队名与符号取反 |

### 2.3 统计与展示

| 项 | 内容 |
| --- | --- |
| 台账 | `DecisionLedger` JSONL 追加写，含买入建议与被拦截盘口 |
| 结算 | 赛程 `ms==110` ⊕ `C109` **并集**判定结束 → 回填终场比分 → 判赢/输/走水/赢半/输半 |
| 正确率 | 命中率 / ROI / CLV；`/ledger/stats` + `/ledger/history` |
| **历史战绩菜单** | **本轮新增**：总览 + 按日累计 + 分联赛/盘口/触发 + 逐条明细（含实际比分） |
| 实时盘口菜单 | 逐场逐盘口：实时赔率/去水/edge/走势/门控结论 |
| 校准报告 | 正确率面板 + Brier/ECE/CLV + 系统状态 |

### 2.4 性能（都是实测过的真实事故）

| 问题 | 修复 |
| --- | --- |
| 启用变动触发后 CPU 165% | 快照缓存**增量合并**（替代整体失效）+ 指纹 TTL |
| 看板冷启动 45s | `match_index()` 只扫目录名（0.09s vs 14s）+ `LiveBook` 内存表 |
| 结算接口 >150s 超时 | 只对**真能结算**的场次捕获收盘价（读盘 8.4 万文件） |
| `/health` 60s 超时、容器 unhealthy | **陈旧先返回 + 后台刷新**（单飞，绝不阻塞） |

### 2.5 安全

`乐鱼app.zip`（含 154 处 token + 账号口令）**已从 git 移除**并入 `.gitignore`。

### 2.6 本轮提交（2026-10-07，分支 `fix/TASK-11-stale-backfill-quotes-not-live`）

建议按此顺序阅读，**每个提交都自洽**（自己的 message 里带根因与实测证据）：

| SHA | 提交 | 一句话 |
| --- | --- | --- |
| `0787dae` | `test(tests)` | 用编译后代码对象替换易碎的 `getsource` 断言 |
| `e4c3c1a` | `fix(realtime)` | 陈旧回填行情不得冒充「进行中」（含 §6.5 的不变量） |
| `f1bb445` | `fix(search_tools)` | 补回 `print_articles` 定义，修必然 `AttributeError` |
| `7a9da66` | `chore(lint)` | 声明 ruff 策略、关停 pi-lens 自动格式化、忽略索引缓存 |
| `2c9149e` | `docs` | 修正环境/测试事实，记录回填行情与工具链事故 |
| `a482c39` | `chore(api)` | 删除未使用的 `threading` 导入 |

本轮验证口令（全部在 `a482c39` 上跑过，**含退出码**）：

```bash
python3 -m unittest discover -s tests -q   # Ran 1115 tests … OK (skipped=11)  exit 0
/root/.pi-lens/tools/node_modules/.bin/pyright           # 0 errors, 0 warnings
/root/.pi-lens/pip-user/bin/ruff check --config pyproject.toml \
    service/analysis.py collector/leyu_realtime.py \
    search_tools/sogou_searcher.py api/app.py tests/     # 改动文件 delta = 0
```

---

## 3. 🔄 进行中 / 未完成

### 3.1 `graded = 0`：正确率统计链路已通，但**缺赛果数据**（P0）

**现象**：`/ledger/stats` 的 `graded=0`、`hit_rate=None`，历史战绩菜单所有比率显示 `—`。

**根因（已定位，非代码问题）**：赛果只在"比赛进行中"那个时间窗可得 ——
实测 `getOriginalDataPB`（赛程）**不返回 `msc`**，而 `structureMatchBaseInfoByMidsPB`
对**已结束**场次返回空串（11/11 全空）。而结算总在结束**之后**。

**已做的修复**：`ScoreStore` 在推送时落盘比分（`output/_live/scores.json`），
实测已攒 **377 场**（其中 39 场 `done=true`）。

**还差什么**：历史遗留的 ~1148 条 pending **补不回来**（上游不再提供数小时前的赛果）。
新比赛结束时会自动结算。**建议**：等 `graded > 0` 后再把命中率当结论用。

**接手人可做**：
```bash
# 看是否有可结算的
curl -s -X POST localhost:8000/api/v1/ledger/settle | python3 -m json.tool
# 检查落盘情况
python3 -c "import json;d=json.load(open('output/_live/scores.json'))['scores'];print(len(d), sum(1 for v in d.values() if v.get('done')))"
```

### 3.2 乐鱼凭据过期（P0，**唯一外部阻塞**）

**现象**：`venue/launch` 返回 `6001 token已过期`；当前靠 `_session.json` 缓存回退维持采集。

**实情**：
- 抓包里那个 token 约 **10 小时后自然过期**（会话生命周期本就如此）；
- **登录续期代码已实现**（`collector/leyu_app_login.py`），但本机 IP 被
  `6031 地区ip限制` 拦住 —— 抓包机 `118.107.172.91` 成功，本机 `38.76.205.122` 被拒；
- 注意 `venue/launch` **不受**该 IP 限制，只有 `user/login` 受限。

**三条出路（任选）**：
1. 在允许的网络/服务器上跑一次登录，把得到的 `x-api-token` 写入 `.env`：
   ```bash
   # .env
   LEYU_APP_TOKEN=<新 token>
   # 或配置账号口令让系统自动续期（需在允许的 IP 上）
   LEYU_APP_LOGIN_NAME=<登录账号>
   LEYU_APP_LOGIN_PASSWORD=<登录口令明文>
   ```
2. 或在该网络部署本服务；
3. 或注入其它可用会话（`LEYU_REQUEST_ID` / `LEYU_SESSION_FILE` / `LEYU_LOGIN_COMMAND`）。

### 3.3 进行中场次与乐鱼"完全一致"（P1）

**现状**：`/analysis` 的 `live` 已给出**同源对账值**：

```json
{"count": 36, "upstream": 42, "derived": 45, "source": "schedule"}
         ↑ 本系统      ↑ 乐鱼页面滚球足球数（同源端点）
```

**为何仍差几场**：上游计数是**秒级变动**的，且"进行中"的判定口径有微小差异
（乐鱼把"已临近开赛但 `ms` 未置 1"也算滚球，本项目用 `grace_s=900` 近似）。
**建议**：不要追求整数相等，而是用 `derived` 与 `upstream` 的**系统性偏差**判断是否异常。

> ✅ **2026-10-07 修**：本条原有另一半隐患——会话过期时 `source=push` 会把
> **回填的陈旧行情**报成"真实进行中"（实测 `count=527`，而推送一条没收到）。
> 已加**行情真实年龄门禁**（`push_quote_max_age_s`，默认 900s），详见 §6.5。
> 现在会话不可用时如实返回 `source=none, count=null` + 错误原因，**不冒充**。

### 3.4 触发吞吐 vs LLM 延迟（P1）

单批 12 场 × LLM 150s ≈ 长时间占用。实测 `pending` 一度达 72。
`change_batch` / `change_min_interval_s` 可调，但**建议改为按 LLM 实际吞吐自适应限流**。

---

## 4. 📋 待完成（含技术债）

### 4.1 技术债实测状态（AGENTS.md §7 台账）

| # | 问题 | 实测状态 | 建议 |
| --- | --- | --- | --- |
| 1 | `config.py` 硬编码 `sk-` token | ❌ **仍存在**（`config.py:9`） | 改 `os.environ.get(..., "")`；**密钥需人工轮换** |
| 2 | `captcha_token` 硬编码 | ❌ **仍存在**（`main.py`、`main_optimized.py`） | 同上 |
| 3 | `.pyc` 入库 | ✅ 已清（0 个被追踪） | — |
| 4 | 无 `.gitignore` | ✅ 已建 | — |
| 5 | 平行副本 | ❌ **3 个仍在**（各有 5~6 处引用） | 需先确认引用再合并/删除 |
| 6 | 0 测试 | ✅ **1115 用例 / 23 文件** | 持续补 |
| 7 | `chromedriver.exe` 入库 | ❌ **仍被追踪**（20MB） | 改由 `webdriver-manager` 容器内获取 |
| 8 | 死分支 | ❌ `origin/feature/add-sportsdata-searcher`、`origin/new` | 确认后清理 |
| 9 | 调试脚本在根目录 | ❌ `搜索.py`、`测试TypeError修复.py`、`验证码识别/` | 归入 `tools/` 或 `tests/` |
| 10 | 运行产物入库 | ✅ 已清（0 个被追踪） | — |
| 11 | 无静态检查配置（ruff） | ⚠️ **本次已声明策略**（`pyproject.toml [tool.ruff]`） | 仅启用真缺陷类（E4/E7/E9/F/B）；**UP 现代化迁移待办**（全仓 2000+ 处 `Optional[X]`→`X \| None`），属独立全仓任务 |
| 12 | 测试目录历史 lint 欠账 | ⚠️ 实测 **36 条 / 13 个测试文件**（`F401` 未用导入为主，1 条 `E731`），**均为本次之前就存在**，且都不在本次改动文件里 | 单独一次 `chore(tests)` 清理；不要混进业务补丁 |
| 13 | 4 处 `inspect.getsource` 断言易碎 | ✅ **已修**（本次） | 改为查**编译后的代码对象**：新增 `tests.referenced_names()`（递归 `co_consts` 进嵌套函数，**刻意不收 `co_varnames`**），4 处断言全部改用它；已用变异测试证明**非空洞**（删掉 `_record_ledger` 调用后检查确实失败） |

### 4.2 功能增强建议（按价值排序）

1. **真实收盘价**：当前 CLV 用"最后一次看到的赔率"近似，非官方收盘价（已在代码注释中声明）。
2. **基本面数据**：LLM 现在只看赔率，本质是"无信息的高价顾问"。接入伤停/首发/近况才能让 edge 有真实来源。
3. **前端真推送**：现在 `/board` 是 10s 轮询，可改 SSE。
4. **门控阈值校准**：`/ledger/history?all=1` 能看到被拦截盘口的表现（`unpicked_hit_rate`），用它判断门控在帮忙还是误杀。
5. **整仓统一格式（可选、一次性）**：仓库当前只有 12/60 个文件符合 `ruff format`；若要统一，应作为**独立的 `style:` 提交**一次做完，而不是让工具每回合局部重排（见 §6.6）。

---

## 5. 运维手册

### 5.1 常见症状 → 处置

| 症状 | 先查什么 | 常见原因 |
| --- | --- | --- |
| 页面空白 / 决策列表 0 场 | `curl /api/v1/decisions` 看 `pending` | 首轮未跑完（一轮全量要 50+ 分钟） |
| 进行中显示"未知" | `curl /api/v1/analysis` 看 `live.error` | 会话失效 + 推送未覆盖 |
| 采集停了 | `curl /api/v1/realtime` 看 `idle_s` | 凭据过期（§3.2） |
| 正确率一直是 `—` | `curl /api/v1/ledger/stats` 看 `graded` | 无赛果（§3.1） |
| 容器 unhealthy | `docker inspect --format '{{json .State.Health}}'` | `/health` 超时（见 §6.4，已修） |
| CPU 打满 | `py-spy dump --pid 1`（见下） | 全量扫盘（见 §6.2、§6.3） |

### 5.2 用 py-spy 看线程在忙什么（本项目最常用的排障手段）

```bash
C=$(docker inspect ai_football_analytics_api --format '{{.Id}}')
docker run --rm --pid=container:$C --cap-add=SYS_PTRACE --security-opt seccomp=unconfined \
  -v /tmp/py-spy:/py-spy:ro alpine:3.20 /py-spy dump --pid 1
```

> 首次需 `docker cp ai_football_analytics_api:/usr/local/bin/py-spy /tmp/py-spy`

### 5.3 关键环境变量

```bash
# 会话（凭据过期时改这里）
LEYU_APP_TOKEN / LEYU_APP_UUID / LEYU_APP_SIGNATURE
LEYU_APP_LOGIN_NAME / LEYU_APP_LOGIN_PASSWORD    # 自动续期（需允许的 IP）
LEYU_H5_TOKEN / LEYU_H5_UUID / LEYU_H5_SIGNATURE
DATA_SOURCE=leyu

# 决策
ANALYSIS_CHANGE_TRIGGER=1        # 盘口变动触发（默认开）
ANALYSIS_CHANGE_DEBOUNCE=30      # 单场防抖秒
ANALYSIS_CHANGE_BATCH=12         # 每批场次
ANALYSIS_CYCLE=600               # 定时兜底间隔
ANALYSIS_LLM_TIMEOUT=150         # LLM 硬超时
ANALYSIS_MAX_PROB_DEVIATION=0.35 # 污染护栏
ANALYSIS_LEAK_SCORE=0            # 1=把比分下发给 LLM（仅对照实验用）
ANALYSIS_SETTLE_INTERVAL=60      # 结算间隔

# LLM
PI_LLM_PROVIDER / PI_LLM_MODEL / PI_LLM_BASE_URL / PI_LLM_API_KEY
```

### 5.4 数据落盘位置

```text
output/
├── snapshots/            # 不可变快照库（10 万+ 文件，按 联赛/场次 分目录）
├── _live/
│   ├── live.json         # 内存实时表落盘（重启回填，~13MB）
│   └── scores.json       # ★ 比分/结束状态（结算的唯一赛果来源）
├── _trends/<mid>.jsonl   # 每场走势（追写）
├── ledger/ledger.jsonl   # 决策台账（结算/统计的唯一依据）
├── decisions.json        # 最近一轮决策结果（重启即恢复）
└── _session.json         # 会话缓存（token 过期后的回退）
```

---

## 6. 踩过的坑（**最有价值的一节**）

> 这些都是**真实故障**，不是假想。多数已被测试锁定，改代码时别把它们改回去。

### 6.1 登录续期：旧结论是错的

`leyu_app_session.py` 原本断言"token 无法自动续期，因为登录有人机验证"。
**抓包推翻了它**：登录请求体 `"Kaptchcate": 99`，服务端**不校验验证码**。
不需要"绕过"，因为它根本没启用。

`x-api-xxx` 也**不需要逆向**：实测是**站点级固定值**
（抓包值与 `.env` 里的都可过，垃圾值 6003，改 body/换路径复用同一签名仍成功）。

### 6.2 有过期价 → 凭空造出注单

实测：某场比分已 **1:3（4 球）**，系统仍建议"买入 全场进球数>2.5 @3.32"。
根因两层：① `compute_markets` 遍历该场**全部 216 条历史快照**（同一盘口算 15 次）；
② 那些价来自 **4.6 小时前**。

修复：`_latest_per_market()` 去重 + `max_quote_age_s=600` 硬门禁。

### 6.3 用快照时效冒充"进行中"

`state == "active"` 只是"**文件不太旧**"，与"比赛是否在踢"无关。
实测页面因此声称 **2436 场进行中**，而乐鱼只有 67 场（差 36 倍）。

修复：`live_known` 判据 —— 拿不到权威来源时标"未知"，不冒充。

### 6.4 `/health` 把探针探死

两个独立问题叠加：① 计算在锁外 → 5+ 线程同时扫盘（8 万文件 × 3s）；
② TTL 到期那次仍要同步等 3~19s，而 Docker `timeout=6s`。
→ 容器被判 unhealthy，探针把服务探死。

修复：**陈旧先返回 + 后台刷新**（单飞）。

### 6.5 回填的实时行情冒充"进行中"（2026-10-07 修）

**现象**：容器重建后 `/api/v1/analysis` 报 `{"source":"push","count":527}`
（自称"真实进行中"），而同一时刻 `/realtime` 明明是
`connected=0, messages=0, price_ticks=0` —— **推送链路完全死的，却报了 527 场**。

**根因（两层）**：
1. `LiveBook.load()` 把每行回填的 `at` 一律写成"载入这一刻"，于是 24 小时前的
   旧行情在 `health()` 里显示成 `newest_age_s=75.8`（谎报新鲜）；
2. `_live_ids_from_push()` 拿 `live_mids()` 的全部结果当活跃场次，没有任何时效门禁。

这与 §6.3 是**同一类错误**（用时效冒充进行中），只是换了"持久化回填"这条路径
重新长出来 —— 说明修完一条路径**不等于**修完这个判据。

**修复**：
* `LiveQuote.quote_age_s` —— 按上游 `ts_ms` 算真实年龄（**不会被回填重置**）；
  缺时间戳返回 `math.inf`（"无法证明新鲜"）；
* `LiveBook.load()` 反推真实 `at`，让 `age_s` / `health()` 不再说谎；
* `LiveBook.live_mids(max_age_s=...)` —— 可按新鲜度取活跃场次；
* `health()` 新增 `quote_newest_age_s` / `fresh_rows`（可观测性）；
* `AnalysisConfig.push_quote_max_age_s`（`ANALYSIS_PUSH_QUOTE_MAX_AGE_S`，默认 900s）
  作为 `_live_ids_from_push()` 的硬门禁；没有任何新鲜行情即返回 `None`（未知）。

> ⚠️ **为何陈旧时返回 `None` 而不是空集**（设计要点，勿改回去）：
> 空集会被下游读作「已确认 0 场进行中」，而“没有任何新鲜行情”并不能证明
> “没有比赛在踢”（可能只是采集断了）。误报 0 会让看板在默认勾选
> 「只看进行中」时**变空白且不告警** —— 正是 `api/app.py` 专门防范的
> 「看板空白」故障。返回 `None` 则复用既有的诚实机制：
> `live_known=False` → 前端显示「未知」+ 原因（即 §6.3 的既定原则）。
> 真正“确认 0 场”的权威来源是**赛程**（`ms==1`），那条路的空集才是结论。

> ⚠️ **副作用（已实测，判断为可接受）**：返回 `None` 会让 `candidates()` 走
> `state=="active"` 回退分支，于是候选场次数变大 —— 容器内同一份陈旧书实测
> `candidates(only_live=True)` 从 **457 → 4058**。代价**有界且不花钱**：
> * **不花 LLM**：这些场次赔率全部过期，`compute_markets()` 先被
>   `max_quote_age_s` 拦空 → `DECISION_AVOID` 提前返回（§6.2 的门禁仍在）；
> * **不写台账**：`computations` 为空 → `record_match()` 的 `keep` 为空 → 0 行；
> * 只多花一次**批量**快照加载（`_snapshots_for_many` 已是一次性指纹校验）。
>
> 🚫 **不要为了压这个数就把 `candidates()` 改成「未知即不活跃」**：
> `decide_list()` 对空候选会返回 `count=0`，而 `_run_cycle_once()` **无条件**
> 执行 `self._latest = res` 并落盘 —— 看板会立刻变成**空列表**，
> 正是 §6.3 与 `api/app.py` 要防的「看板空白」。真要改，必须先让
> `_run_cycle_once()` 拒绝用空结果覆盖已有结果（本项未做，留作后续）。

**验证**（真实数据，173,792 行 / 24.3 小时前的 `_live/live.json`）：

| 观测量 | 修复前 | 修复后 | 判据 |
| --- | --- | --- | --- |
| `live_book.newest_age_s` | `75.8`（谎报） | `90030.9`（真话） | 单元测试 + 容器实测 |
| `live_book.quote_newest_age_s` | — | `90030.8` | 按上游 `ts_ms`，不可被回填重置 |
| `live_book.fresh_rows` | — | `0 / 173792` | 新鲜条目计数 |
| `live_match_ids()` | 527 个陈旧 mid | `None` | → `live_known=False` |
| `/analysis` → `live.count` | `527`（冒充） | `null`（未知） | 前端渲染「未知」+ 原因 |
| `/analysis` → `live.source` | `push` | `none` | 不确定就不冒充权威来源 |

**教训**：回填/缓存这类"数据搬运"路径最容易把**时间信息**丢掉，
而时间一旦丢失，下游所有"新鲜度"判断都会静默变成"永真"。
新增任何回填都要问一句：**年龄还算得出来吗？**

> ℹ️ 上表的年龄是**会随时间变大**的（`_live/live.json` 停止更新后就一直在变老）。
> 第二次重建容器后实测已是 `92283.3` —— 与 `90030.9` 同量级即正常，
> 不要把它当成”复现不一致“。真正要盯的是**旧实现的那个 `75.8`**：
> 它永远停在几十秒，那才是“被回填抹掉了年龄”的特征。

### 6.6 其余（简表）

| 坑 | 教训 |
| --- | --- |
| `result_path` / `ledger_root` 后注入失效 | `__init__` 时读配置的方法，运行期改配置**不会自动重跑** |
| `IncompleteRead` 穿透 | 它的 MRO 不继承 `URLError`/`OSError`，必须**显式**写进 `except` 元组 |
| 中文标签改带队名 | 提示词与**解析器**必须用同一套标签，否则 LLM 中文作答会被静默丢弃 |
| 追加依赖时忘 import | 新增 helper 后跑 `py_compile` + `pyright` 立即暴露 |
| 时间线按 `settled_at` 分组 | 结算是批量任务，同批会堆到"今天" → 应按**决策日期**分组 |
| `MagicMock(name=..., **{...})` | pyright 会报错（`name` 是第一个位置参数）→ 用简单 stub 类 |
| 仓库无 ruff 配置 → 外部工具套用**自己的规则集** | 实测对一个从未采纳 UP 规则的仓库报出近 **3000 条**阻断项，把真缺陷淹没；显式声明 `[tool.ruff]` 后立刻在 `sogou_searcher.py` 找到 **3 处真实未定义名**（`return` 之后的死代码）。教训：**"工具没报错" 不等于 "代码没问题"** —— 要先问「它在按谁的规则报」 |
| `return` 之后残留大段代码 | Python 不会报错（而是变成死代码），只有 lint 的未定义名规则能抓到。新增 `return` 时顺手删干净后续块 |
| 文档里的"环境事实"过期 | AGENTS.md §0 曾误记「宿主机 Python 3.12.3 / 无 pip / bs4+numpy+matplotlib+pandas 可用」，实测全错（3.11.2 / pip 23.0.1 但 PEP668 / 只有 requests）。**已修正**。教训：「文档说行」不等于行 —— 改环境相关流程前**先实测一次** |
| 用不挂字体的容器重跑图表 | 会让中文变方块并**静默覆盖已入库图片**（需 `git checkout` 还原）。容器重跑图表必须 `-v /usr/share/fonts/truetype/wqy:...:ro`，见 AGENTS.md §1.1A |
| 皮试工具（pi-lens）**在 agent_end 自动重排被改过的文件** | 本仓库最坑的一条。实测：它在一个回合结束对 6 个 `.py` 跑 `ruff format`，把一个语义改动 120 行的文件变成 **581 行 diff**（`api/app.py` 只改了 1 行 import → 也变成 **600 行 diff**），把补丁彻底淹没；更阴的是它会**把仓库风格改得不一致** —— 实测全仓 60 个文件里只有 **12 个**是 ruff-format-clean，即仓库**本来就不采纳**这套格式。它还会**静默改变文件内容**，使正在跑的测试出现幻影结果（见上一行）。**已在 `.pi-lens.json` 里 `format.enabled=false` 关闭**（**实测已失效**：一次回合里改了 3 个测试文件，其中 2 个确实是 format-dirty（`ruff format --check` 报 would reformat），但回合结束后 8 个相关 `.py` 的 `md5sum` **全部未变**、`recent-touches.json` 也没新增 `reason:"format"` 条目）；若要整仓统一格式，应当是**独立的一次性任务**，不是每回合的副作用 |
| 跑测试**期间**文件被改写 | `inspect.getsource()` 用**编译时冻结的行号**去读**当前磁盘文件**，文件一旦在测试跑的过程里被整体重排，就返回**别的函数体** → 断言**假失败**（实测 `test_cycle_writes_ledger` 报 `_record_ledger not found`，而 `decide_list` 里明明有；冻结代码后单独复跑即 `OK`）。反方向同样危险：断言可能被**碰巧满足**而**假通过**。**已根治**：`tests/` 原有 4 处 `getsource` 断言全部改为查**编译后的代码对象**（`tests.referenced_names()`），不再依赖“行号↔磁盘文件”一致；变异测试证明删掉调用后检查会失败（非空洞）。但**其余测试仍应冻结跑**（测试本身也是文件），且新代码不要重新引入 `getsource` 式断言 |
| 改了代码却没重建镜像 | 容器跑的是**构建时**烤进去的副本（只有 `output/` 是挂载卷）。实测主机与容器 `/app` 的 `service/analysis.py` md5 **不一致** —— 也就是说当时“在容器里验证过”的其实是**旧设计**（容器里还是“陈旧→空集”的中间版，主机已是“陈旧→None”）。**改完代码必须 `docker compose up -d --build`**，并用 `docker exec <容器> md5sum /app/<文件>` 与主机对一次；否则验证结论无效 |

---

## 7. 交付物索引

| 类型 | 路径 |
| --- | --- |
| **本文档** | `docs/HANDOVER.md` |
| 修复报告（图文） | `reports/dashboard-data-stats-optimization/REPORT.md` / `.html` |
| 可行性调研（范本） | `reports/leyu-kaiyun-odds-bot-feasibility/REPORT.md` |
| 乐鱼协议文档 | `docs/architecture/leyu-api-protocol.md` |
| 项目宪章（**必读**） | `AGENTS.md` |
| 测试 | `tests/`（23 文件 / 1115 用例） |

---

## 8. 接手检查清单

- [ ] 按 §1.1 启动（**记得 4 个端口变量**：6380 / 8081 / 8000 / **3001**）
- [ ] §1.2 四项验证全过
- [ ] 跑 `python3 -m unittest discover -s tests -q`（应 `Ran 1115 … OK`；宿主机可直跑，**不需** pytest）
- [ ] 跑 `pyright`（应 0 errors）
- [ ] 确认 `.pi-lens.json` 仍在（`format.enabled=false`）—— 它挡住"每回合自动重排文件"，
      那正是让测试出现**幻影结果**的成因（§6.6）；删掉它会让下一次测试跑动中文件被改
- [ ] 确认凭据状态（§3.2），必要时按三条出路之一处理
- [ ] 确认 `graded` 是否开始增长（§3.1）
- [ ] 读 §6（踩过的坑），改代码时别把已修的坑改回去 —— 尤其 §6.5 的 `None` 不变量
- [ ] 若本分支尚未合并：先走 PR（AGENTS.md §2.4：业务代码**不直推 main**）
