# AGENTS.md — ai_football (Multi-Agent Operating Protocol)

足球赛事数据采集 / 文章智能分析 / 研究报告产出项目（**Python 3** 后端 + 轻量 Web 前端）。
本文件是所有接入本仓库的自主 Agent（Codex、Pi、Claude Code、OpenClaw 等）与各工作节点的**最高运行宪章**。

> **本文件由 `/root/llm/AGENTS.md`（Go + React 项目）改写而来**，已按本项目真实技术栈、目录结构与历史事故重新映射。
> 沿用其治理骨架：运行环境强约束、分支安全、反屎山红线、反偷懒循环、图文并茂交付、DoD 检查清单。

---

## 0. 📌 本项目事实基线（改动前必读）

| 维度 | 实情 |
| --- | --- |
| 语言 / 运行时 | **Python 3.12**（`python3 -V` 实测 3.12.3）；使用 `#!/usr/bin/env python3` |
| 依赖声明 | `requirements.txt`（仅 `requests`、`beautifulsoup4`）；浏览器链路见 `docker/requirements-browser.txt` |
| 浏览器自动化 | Selenium 4 / `undetected-chromedriver` / `webdriver-manager`，运行在 **Docker 沙盒容器**内 |
| 采集架构 | `browser_scraper_client.py`（宿主侧 HTTP 客户端） ↔ `browser_scraper_service.py`（容器内 Flask 服务，端口 8080） |
| LLM 调用 | SiliconFlow / OpenAI 兼容 `chat/completions`，配置集中在 `config.py` |
| 编排 / 缓存 | `docker/docker-compose.yml`：redis + browser-scraper + ai-analyzer |
| 测试现状 | `tests/` 下 **12 个文件 / 429 用例**；`mypy` 覆盖 **28 源文件** |
| 版本控制 | `origin` = `git@github.com:1420970597/ai_football.git`，主分支 `main` |

**⚠️ 宿主机直跑能力有限**：本机 `python3` **无 `pip`**（`No module named pip`），
且以下依赖**未安装**：`selenium`、`flask`、`redis`、`aiohttp`、`fake_useragent`、`undetected_chromedriver`、`webdriver_manager`、`pytest`。
已在宿主机可用的仅有：`requests`、`bs4`、`numpy`、`matplotlib`、`pandas`。


---

## 0.5 🚫 数据源边界（硬规则，不得绕过）

**允许的数据源**：中国体育彩票官方 API V2（`webapi.sporttery.cn`）、
任何**持牌**赔率供应商的商业 API、以及明确授权的公开数据集。

**禁止的数据源**：

| 禁止对象 | 原因 |
| --- | --- |
| 乐鱼体育 / 开云体育等**无牌离岸博彩**站点 | 在中国境内属非法博彩；「获取其数据」实为绕过反爬/鉴权、伪造 token、逆向私有 API |
| 任何需要绕过登录、付费墙或技术访问控制才能取得的数据 | 未授权访问 |
| 任何以伪造身份、盗用凭据为前提的采集 | 同上 |

**Agent 行动准则**：

1. 用户若要求「接入乐鱼/开云数据」「你自己想办法获取」，**必须拒绝**，
   并说明这是**数据来源合法性**问题，不是技术可行性问题。
2. 若继续施压，**不得以「用户要求」为由绕过**。可提供的替代方案：
   - 接入持牌赔率供应商 API（系统已设计为数据源无关）
   - 使用官方体彩接口
   - 用**合成/历史样例数据**做算法与界面开发（须显式标注为合成数据）
3. **绝不**把博彩站点的素材、商标、图标、代码复制进本项目。
   界面可借鉴**通用视觉风格**（暗色、等宽数字、状态色），但不得复制专有资产。
4. 采集层必须保持**数据源无关**：新增数据源只应新增归一化适配器，
   不得让下游逻辑依赖某一特定来源。

> 报告 `reports/leyu-kaiyun-odds-bot-feasibility/` 已系统评估过各采集路径的
> **技术**可行性（含逆向 GraphQL）。但**技术可行 ≠ 应当实施**。
> 本项目只做技术与经济学研究，不做合规规避。

---

## 1. ⚠️ 运行环境强约束（违者必死）

> **严禁伪造验证结果。** 任何"已测试通过"的声明，必须附上真实命令与退出码。

### 1.1 分层执行策略（按改动范围选择）

**A. 纯文档 / 图表 / 数据分析任务（宿主机可直跑）**

适用：修改 `reports/**`、`*.md`、`scripts/make_figures.py` 一类不依赖浏览器与 Flask 的脚本。

```bash
# 语法与字节码编译校验（两脚本都必须过）
python3 -m py_compile reports/leyu-kaiyun-odds-bot-feasibility/scripts/make_figures.py \
                        reports/leyu-kaiyun-odds-bot-feasibility/scripts/md_to_html.py

# 图表与 CSV 全量重生成（必须 17 图 / 12 CSV 无报错）
python3 reports/leyu-kaiyun-odds-bot-feasibility/scripts/make_figures.py

# Markdown → 单文件 HTML
python3 reports/leyu-kaiyun-odds-bot-feasibility/scripts/md_to_html.py \
        reports/leyu-kaiyun-odds-bot-feasibility/REPORT.md \
        reports/leyu-kaiyun-odds-bot-feasibility/REPORT.html
```

**B. 爬虫 / 分析器业务代码（强制走 Docker，宿主机依赖不全）**

```bash
# 语法检查（宿主机即可，不 import 第三方）
python3 -m py_compile main.py enhanced_analyzer.py match_generator.py \
                        browser_scraper_client.py browser_scraper_service.py config.py

# 依赖完整性校验（必须进容器，宿主机缺 selenium/flask/redis）
docker run --rm -v "$PWD:/w" -w /w python:3.12-slim sh -c \
  "pip install -q -r requirements.txt && python -c 'import requests, bs4; print(\"base deps OK\")'"
```

**C. 浏览器链路 / 全栈集成（必须走 compose）**

```bash
make compose-up      # 若 Makefile 缺失则直接： docker compose -f docker/docker-compose.yml up --build
docker compose -f docker/docker-compose.yml ps          # 三容器必须全部 healthy
curl -sf http://localhost:8080/health                    # browser-scraper 健康检查必须 200
```

### 1.2 三条硬性禁令

1. **禁止在宿主机 `pip install`**（本机无 pip；且会污染宿主环境）。需要新依赖 → 写入 `requirements.txt` / `docker/requirements-browser.txt`，在容器内验证。
2. **禁止用 `chromedriver.exe` 在 Linux 宿主机直跑**。仓库根目录的 `chromedriver.exe`（20MB，Windows 二进制）仅供 Windows 侧使用；Linux 路径必须走 `docker/Dockerfile.browser` 的 Chrome 沙盒。
3. **禁止在没有 Docker 的情况下声称"已验证爬虫链路"**。若 daemon 不可用，必须显式声明"未验证"并说明原因，不得含糊。

---

## 2. 🔀 多机协同与分支安全协议（治理分支失控）

当前仓库存在**历史遗留死分支**：`origin/feature/add-sportsdata-searcher`、`origin/new`。
严禁再新增无序分支。

1. **分支命名与绑定格式**（必须绑定任务）：

   | 类型 | 格式 |
   | --- | --- |
   | 新功能 | `feat/TASK-<编号>-<英文简短描述>` |
   | 缺陷修复 | `fix/TASK-<编号>-<英文简短描述>` |
   | 调研 / 设计 / 报告 | `docs/TASK-<编号>-<英文简短描述>` |
   | 爬虫采集器 | `feat/TASK-<编号>-collector-<站点名>` |

2. **作业前同步基线**：

   ```bash
   git fetch origin main
   git checkout -b <规范分支名> origin/main
   ```

3. **分支清理生命周期**：单分支单任务；PR 合入 `main` 后**立即**清理：

   ```bash
   git push origin --delete <规范分支名>
   git branch -D <规范分支名>
   ```

4. **默认工作流**：本项目历史上以直推 `main` 为主（见 `git log`）。**小范围文档/报告改动允许直推 `main`**；涉及业务代码或破坏性变更**必须走 PR**。

---

## 3. 🛡️ 架构红线：严禁制造屎山代码（Anti-Spaghetti Code）

> **本节针对本仓库已存在的真实劣化模式。** 以下每一项都已在仓库中发生过，属于**必须停止并逐步清理**的技术债。

### 3.1 严禁平行文件与副本函数（❌ 本仓库已违规）

- ❌ `main.py`（151KB）与 `main_optimized.py`（64KB）**并存**，后者是前者的"优化副本"。
- ❌ `enhanced_analyzer.py` 与 `web/enhanced_analyzer.py`、`web/match_generator.py` 与根目录同名文件**重复**。
- ✅ 正确做法：**直接重构原函数**，同步修改所有调用方。确需保留历史版本用 `git`，不用文件副本。
- ✅ 清理 `web/` 副本时，**先确认无外部引用**再删除；删除需在 commit message 说明替代路径。

### 3.2 分层架构职责边界

| 目录 / 文件 | 职责 | 严禁 |
| --- | --- | --- |
| `browser_scraper_service.py` | 容器内浏览器渲染与页面抓取服务（Flask，:8080） | 严禁在其中写分析逻辑或 LLM 调用 |
| `browser_scraper_client.py` | 宿主侧 HTTP 客户端（重试、批量、健康检查） | 严禁在其中直接 `import selenium` |
| `search_tools/` | 各站点检索器（`base_searcher.py` 为基类） | 严禁绕过基类重复实现重试/限速 |
| `web/*爬虫/main.py` | 单站点采集入口 | 严禁在此层做跨站点聚合 |
| `enhanced_analyzer.py` / `match_generator.py` | 分析与生成业务 | 严禁在内部硬编码 URL / 密钥 |
| `config.py` | **唯一**配置来源 | 严禁在其他文件重复定义配置字典 |
| `验证码识别/` | 验证码与验证页处理 | 严禁把一次性调试脚本长期留在仓库根 |

**新增采集器必须继承 `search_tools/base_searcher.py`**，`search_tools/sportsdata_searcher.py` 是参照实现。

### 3.3 禁止半成品代码（TODO Mocking）

- 核心链路严禁 `// TODO: implement later`、`pass  # 稍后实现`、`raise NotImplementedError`（基类抽象方法除外）。
- 严禁返回假数据却标注为"已实现"。占位数据必须显式命名（如 `_PLACEHOLDER_`）并在日志中告警。

### 3.4 🔐 密钥与敏感信息（❌ 本仓库已违规）

> **已确认仓库中存在硬编码凭据，且随 `git` 历史泄漏。**

| 位置 | 内容 |
| --- | --- |
| `config.py` → `API_CONFIG.api_token` | 明文 SiliconFlow API Token（`sk-` 开头） |
| `main.py:149`、`main_optimized.py:390` | 明文 `captcha_token` |
| `__pycache__/config.cpython-312.pyc` | **被 git 追踪**，同样泄漏 token |

**强制规则：**

1. **新增代码一律从环境变量或 `.env` 读取**（`docker-compose.yml` 已用 `environment:` 注入，可参照）。
2. `config.py` 中凭据必须改为 `os.environ.get("SILICONFLOW_API_TOKEN", "")` 形式，**默认值必须为空字符串**。
3. 严禁提交 `__pycache__/`、`*.pyc`、`output/` 运行产物（见 §3.5）。
4. **不要求 Agent 擅自改写已泄漏的密钥**（需人工轮换）；但**发现新硬编码凭据必须立即上报**，并在本次改动中不再新增。

### 3.5 🚫 严禁提交运行产物（❌ 本仓库已违规）

仓库当前**无 `.gitignore`**，导致：

- `__pycache__/*.pyc` 被追踪（7 个文件，含泄漏 token 的 `config.cpython-312.pyc`）
- `output/` 与 `web/*/output/*.json`、`web/*/返回信息.txt` 等运行产物被追踪（9 项）

**强制动作**：新增文件提交前必须确认不属于以下模式：

```gitignore
__pycache__/
*.pyc
.env
output/
web/*/output/
web/*/返回信息.txt
*.log
.idea/
```

> 清理已追踪的产物需用 `git rm --cached <path>`（保留本地文件），并在 commit message 中说明。

---

## 4. 🔄 任务全生命周期循环（Anti-Laziness Protocol）

针对 Agent "遇到复合任务只做 1~2 项就停滞"、"假装完成"的偷懒行为，必须执行状态机驱动循环。

1. **显式任务状态追踪表** — 接收到多项 TODO 的复合指令时，**每次输出最前端**必须给出状态矩阵：

   ```text
   [Task Tracker]
   - Total Tasks: N
   - Completed: [任务A (已自测通过), 任务B (已自测通过)]
   - Active Task: [任务C]
   - Remaining: [任务D, 任务E, ...]
   ```

   > 本仓库优先使用 `todo` 工具维护；上述文本矩阵为无工具环境下的等价形式。

2. **强制自动推进（Non-stop Loop）** — 只要 `Remaining` 非空，**严禁**停下来询问"是否继续"或只汇报部分完成。必须自动切换下一个 Active Task。

3. **自测闭环要求**：

   - 全仓库目前 **0 测试**。**任何新增或重构的业务模块，必须同目录配套 `test_<module>.py`**。
   - 用例必须覆盖**至少一条正常路径 + 一条边界/异常路径**（如：requests 超时、HTTP 非 200、HTML 结构变更导致解析为空）。
   - 图表脚本改动需覆盖**参数边界**（如空数组、除零、格式串 `%` 转义）。
   - 因宿主机无 `pytest`，测试须可在容器内运行：

     ```bash
     docker run --rm -v "$PWD:/w" -w /w python:3.12-slim sh -c \
       "pip install -q pytest requests beautifulsoup4 && python -m pytest -v"
     ```

---

## 5. 📑 调研、设计与原型任务规范（必须配套图文并茂的 Markdown 文档）

任何包含"竞品调研"、"系统设计/技术方案"、"可行性研究"、"页面原型设计"的工作，**严禁仅在终端打印散装文字**，必须同步生成或更新配套 Markdown 交付文档。

### 5.1 存放路径规范

| 类型 | 路径 |
| --- | --- |
| 调研 / 可行性报告 | `reports/<topic>/REPORT.md` |
| 架构 / 技术方案设计 | `docs/architecture/<system-or-module>.md` |
| UI / 交互与原型设计 | `docs/prototypes/<feature-name>.md` |

> **本项目既有约定**：研究报告采用 **每主题独立目录**（`reports/<topic>/`），内含
> `README.md`、`REPORT.md`、`REPORT.html`、`images/`、`data/`、`scripts/`。
> 现有范本：`reports/leyu-kaiyun-odds-bot-feasibility/`。**新增报告必须对齐该结构。**

### 5.2 "图文并茂"的硬性判定标准（违者判定为未完成）

纯文本罗列、缺乏结构化视图的文档一律打回。交付文档必须包含以下要素：

1. **架构与逻辑图（必须使用原生 Mermaid 代码块渲染）**：

   | 用途 | 图类型 | 本项目示例 |
   | --- | --- | --- |
   | 数据流与时序 | `sequenceDiagram` | 赔率跳动事件从球场到成交的全链路 |
   | 状态机流转 | `stateDiagram-v2` | 下注单生命周期（待发/已发/接受延迟/改价/拒单/成交） |
   | 系统关系 / 模块分层 | `graph TD` 或 `flowchart LR` | LLM + JEV + 经济学算法分层 |
   | 决策路径 | `graph TD` | 采集路径选择、模型路由 |

   > **渲染契约**：`scripts/md_to_html.py` 必须把 ` ```mermaid ` 围栏转为
   > `<pre class="mermaid">` 并注入 Mermaid 运行时；离线时必须**优雅降级**为可读代码块，
   > 不得出现空白。**新增 Mermaid 前先确认渲染链路可用。**

2. **对比与评估矩阵表（Markdown Tables）**：

   - 调研类文档必须包含多维评估矩阵表，**对比不少于 3 个市面成熟方案**，
     横向涵盖 **6 个以上核心维度**（本项目建议：接入层级、延迟表现、盘口完整性、
     稳定性、维护成本、覆盖率、数据标准、自研成本、依赖风险）。

3. **高保真原型布局图（UI / 原型类专属要求）**：

   - 必须提供 **ASCII/Markdown 字符线框图**（Header、Sidebar、Content 区域、表单字段、关键操作按钮的完整空间分布）。
   - 必须涵盖 **5 大界面状态定义表**：

     | 状态 | 要求 |
     | --- | --- |
     | `Default` | 常规完整数据态 |
     | `Loading` | 骨架屏 / 加载指示设计 |
     | `Empty` | 缺省状态文案与行动倡导按钮 |
     | `Error` | 网络错误 / 校验失败 Toast 或 Banner 交互 |
     | `Edge-Case` | 长文本截断、极值数据溢出等适配规则 |

4. **配图与视觉参考标准**：

   - 引用现有界面做对比分析时，以引用格式说明参考来源与界面示意。
   - **涉及量化结论的图表必须可复现**：脚本落在 `scripts/`，数据落在 `data/`（CSV），
     且**所有关键参数必须是命名常量**（禁止魔法数字散落在表达式里）。
   - 统计图必须使用中文字体（本项目已验证 `WenQuanYi Zen Hei` `/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc`），
     并设置 `axes.unicode_minus = False`，避免负号与中文变方块。

### 5.3 数据与结论的诚实性要求（本项目特有）

- **必须标注证据分级**（A 一手文档/可核验代码 · B 学术/行业聚合 · C 社区实测）。
- **必须区分"测量值"与"主观评分"**：主观评分需在文中显式声明并落到 CSV 供复核。
- **必须给出量级示意参数的敏感性方向**（如"改为 5% 或 50% 重跑，结论方向不变"）。
- **不得把经验估算包装成精确值**；估算锚点须可核验（如 CIES 的 330,748 场/12 赛季）。
- **调研中发现的信息污染必须显式披露**（如把 SEO 内容农场识别为不可信信源）。

---

## 6. 🏁 最终交付检查清单（Definition of Done）

提交或汇报完成前，**必须依次跑通**对应流水线并附退出码：

```bash
# 1. Python 语法与字节码编译（业务代码 + 脚本，全绿）
python3 -m py_compile *.py search_tools/*.py web/*/main.py \
                        reports/*/scripts/*.py

# 2. 依赖完整性（业务代码改动时，容器内验证；宿主机无 pip）
docker run --rm -v "$PWD:/w" -w /w python:3.12-slim sh -c \
  "pip install -q -r requirements.txt && python -c 'import requests, bs4'"

# 3. 单元测试（新增/重构业务模块时必须存在；宿主机无 pytest 故走容器）
docker run --rm -v "$PWD:/w" -w /w python:3.12-slim sh -c \
  "pip install -q pytest requests beautifulsoup4 && python -m pytest -v"

# 4. 浏览器链路集成（涉及采集改动时，三容器必须 healthy）
docker compose -f docker/docker-compose.yml up -d --build
docker compose -f docker/docker-compose.yml ps
curl -sf http://localhost:8080/health && echo " scraper OK"

# 5. 报告可复现性（涉及 reports/ 改动时，必须全量重生成且数量对得上）
python3 reports/*/scripts/make_figures.py     # 期望 17 图 / 12 CSV
python3 reports/*/scripts/md_to_html.py reports/*/REPORT.md reports/*/REPORT.html

# 6. 文档完整性自检
#    - reports/*/REPORT.md 含原生 Mermaid（sequenceDiagram / stateDiagram-v2 / flowchart）
#    - 含 ≥3 方案 × ≥6 维度的评估矩阵
#    - HTML 中 Mermaid 与全部图片渲染正常（用浏览器实测，非仅看文件大小）

# 7. 卫生检查（严禁提交产物与密钥）
git status --short
git diff --cached --name-only | grep -E '__pycache__|\.pyc$|^output/|返回信息\.txt' && echo "❌ 产物入库" && exit 1
git grep -nE '(api[_-]?key|token|secret)\s*[:=]\s*["'"'"'][A-Za-z0-9_-]{16,}' -- '*.py' && echo "⚠️ 硬编码凭据" && exit 1

# 8. 提交
git add .
git commit -m "<type>(<scope>): <简明中文描述本次改动的核心改动>"
```

**Git 提交类型（Conventional Commits）**：

| type | 用途 |
| --- | --- |
| `feat` | 新增业务功能（采集器、分析器、服务端点） |
| `fix` | 修复具体缺陷（**必须说明根因**） |
| `refactor` | 重构现有架构（不改变外部行为） |
| `test` | 补齐或修复测试用例 |
| `docs` | 架构方案、调研报告、图文原型或设计文档 |
| `chore` | 构建、依赖、`.gitignore`、清理产物等杂项 |

**本项目已验证的提交风格**（沿用）：`docs(reports): v3.0 补充爬虫可行性、赛事覆盖完整性…`

---

## 7. 📎 附：已知技术债台账（Agent 不得扩大，需逐步偿还）

| # | 问题 | 位置 | 处置 |
| --- | --- | --- | --- |
| 1 | 硬编码 API Token | `config.py` | 改环境变量；**密钥需人工轮换** |
| 2 | 硬编码 captcha token | `main.py:149`、`main_optimized.py:390` | 同上 |
| 3 | `.pyc` 入库且含泄漏 token | `__pycache__/*.pyc` | `git rm --cached` + `.gitignore` |
| 4 | 无 `.gitignore` | 仓库根 | 建立并覆盖 §3.5 模式 |
| 5 | 平行副本文件 | `main_optimized.py`、`web/enhanced_analyzer.py`、`web/match_generator.py` | 确认引用后合并/删除 |
| 6 | ~~0 测试~~ | 全仓库 | ✅ 已建 `tests/`（12 文件 / 429 用例） |
| 7 | Windows 二进制入库 | `chromedriver.exe`（20MB） | 评估改由 `webdriver-manager` 容器内获取 |
| 8 | 死分支残留 | `origin/feature/add-sportsdata-searcher`、`origin/new` | 确认后清理 |
| 9 | 调试脚本遗留根目录 | `测试TypeError修复.py`、`搜索.py`、`验证码识别/*.py` | 归入 `tests/` 或 `tools/` |
| 10 | 运行产物入库 | `output/`、`web/*/output/`、`返回信息.txt` | `git rm --cached` |

> **Agent 行动准则**：处理任务时若触及上表条目，**顺手偿还**（同一次 commit 内说明）；
> 但**严禁在未确认引用关系前删除他人文件**，也**严禁扩大**任一技术债。
