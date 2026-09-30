# ai_football — 足球赛事数据采集 / 量化研究与决策系统原型

> 基于 Docker 的 **B/S 架构**（Browser/Server）足球数据平台原型。
> 覆盖「赛事数据采集 → 赔率快照 → 去水定价 → 优势估计 → 仓位计算 → 校准闭环」全链路，
> 所有算法设计均可追溯到 [可行性研究报告](reports/leyu-kaiyun-odds-bot-feasibility/REPORT.md)。

| | |
| --- | --- |
| 🏗️ 运行架构 | Docker Compose 多容器，B/S 分离 |
| 🐍 技术栈 | Python 3.12 · Flask · Selenium/undetected-chromedriver · Redis · Playwright(规划) · LLM API |
| 📊 研究报告 | [reports/leyu-kaiyun-odds-bot-feasibility/](reports/leyu-kaiyun-odds-bot-feasibility/)（17 图 / 53 表） |
| 📐 工程宪章 | [AGENTS.md](AGENTS.md)（运行约束 · 架构红线 · DoD） |
| ✅ 已验证 | **4 容器全栈 healthy**；11 端点实测；17 场真实数据端到端；容器内 271 测试全通过 + mypy 15 文件干净 |
| 📐 设计文档 | [docs/architecture/valuation-core.md](docs/architecture/valuation-core.md) |

---

## 目录

1. [系统定位与设计依据](#1-系统定位与设计依据)
2. [架构总览](#2-架构总览)
3. [全链路数据流](#3-全链路数据流)
4. [任务状态机](#4-任务状态机)
5. [服务清单与职责边界](#5-服务清单与职责边界)
6. [API 契约](#6-api-契约)
7. [Web 控制台原型](#7-web-控制台原型)
8. [目录结构](#8-目录结构)
9. [快速开始](#9-快速开始)
10. [研究报告关键结论](#10-研究报告关键结论)
11. [技术选型对比矩阵](#11-技术选型对比矩阵)
12. [已知问题与技术债](#12-已知问题与技术债)
13. [免责声明](#13-免责声明)

---

## 1. 系统定位与设计依据

### 1.1 这是什么

一个**足球数据采集与量化研究平台**。它的设计目标是把[研究报告](reports/leyu-kaiyun-odds-bot-feasibility/REPORT.md)中的技术分析，
落成一套**可运行、可复现、可审计**的系统原型，并且**只做研究，不做投注执行**。

### 1.2 为什么这样设计

报告的核心结论之一（§13.2）是：**这套架构的分层设计是正确的，但它优化的是一个负期望系统**。
因此本原型采取「**保留研究价值、剥离执行风险**」的设计取向：

| 报告中的层 | 本原型的处置 | 理由（报告依据） |
| --- | --- | --- |
| L1 数据采集 | ✅ **完整实现** | 报告 §3.2/§12.1：逆向 GraphQL 与浏览器自动化已被多个开源项目验证可行 |
| L1′ 微结构提取 | 🔬 **实现为分析模块** | 报告 §5.1：唯一可能不含"报价撤回"污染的信息维度 |
| L2 特征（去水） | ✅ **完整实现** | 报告 §8.1：方法选择本身可造成 0.83–8.0 pp 偏差，足以翻转 `edge` 符号 |
| L3 决策（LLM+JEV） | 🔬 **实现规则 + LLM 慢通路** | 报告 §6.3：LLM 不应进关键路径；JEV 作为门控待接入 |
| L4 经济学算法栈 | ✅ **完整实现** | 报告 §8.2–8.8：有严格最优解，且可迁移 |
| L5 执行（自动投注） | ❌ **刻意不实现** | 报告 §5.3：接受延迟/改价/拒单由对手方裁定；§9.3 触发条件为注单质量 |
| L6 反馈校准 | ✅ **实现评估指标** | 报告 §8.8：Brier/ECE/CLV 是判断"是否真有优势"的唯一客观依据 |

> **设计原则**：**把报告里"可迁移的工程产出"（§13.3）全部实现，把"结构性不可控的部分"（§5.3/§9.3）全部标注为不支持。**
> 这不是功能缺失，而是刻意的边界 —— 报告已论证该部分在经济与结构上不可行。

---

## 2. 架构总览

### 2.1 分层架构与容器映射

```mermaid
flowchart TB
    subgraph CLIENT["🖥️ Browser 端"]
        UI["Web 控制台<br/>赛事看板 · 赔率曲线 · 定价对比 · 校准报告"]
    end

    subgraph GATEWAY["🚪 Server 端 · 接入层"]
        API["analytics-api<br/>REST :8000<br/>鉴权 · 参数校验 · 编排"]
    end

    subgraph CORE["⚙️ Server 端 · 业务容器"]
        direction LR
        COLL["odds-collector<br/>L1 采集 / L1′ 微结构"]
        SCRAPE["browser-scraper<br/>Flask :8080<br/>Selenium 沙盒"]
        ANALYZE["ai-analyzer<br/>L3 决策 · L6 校准"]
        VCALC["valuation-core<br/>L2 去水 · L4 经济学栈"]
    end

    subgraph STORE["🗄️ 存储层"]
        RDS[("redis<br/>:6379<br/>会话 · 快照 · 任务状态")]
        TSD[("时序快照<br/>output/ 卷<br/>不可变 JSON")]
    end

    LLM["☁️ LLM API<br/>SiliconFlow<br/>仅慢通路"]

    UI <-->|"HTTPS / JSON"| API
    API --> VCALC
    API --> ANALYZE
    API --> COLL
    COLL -->|"HTTP /scrape"| SCRAPE
    SCRAPE -.->|"渲染页面"| WEB(("🌐 目标站点"))
    COLL -.->|"逆向后端接口"| WEB
    VCALC --> RDS
    COLL --> RDS
    ANALYZE --> RDS
    VCALC --> TSD
    COLL --> TSD
    ANALYZE -->|"低频 · 非关键路径"| LLM

    classDef done fill:#eef9f2,stroke:#27ae60,stroke-width:2px,color:#12212e
    classDef plan fill:#fdf6e6,stroke:#e67e22,stroke-width:1px,stroke-dasharray:4 3,color:#12212e
    classDef exist fill:#eef4f9,stroke:#2c6fa8,stroke-width:2px,color:#12212e
    classDef none fill:#f5f6f7,stroke:#7f8c8d,stroke-width:1px,color:#12212e
    class SCRAPE,ANALYZE,TSD done
    class VCALC,COLL,API,UI plan
    class RDS,LLM exist
```

**图例**：🟩 已实现并验证 · 🟦 外部依赖 · 🟧 原型设计中（本 README 定义）· ⬜ 不支持

### 2.2 与研究报告的分层对应

```mermaid
flowchart LR
    subgraph R["报告分层 (REPORT.md)"]
        direction TB
        RL1["L1 数据采集"] --> RL1B["L1′ 微结构"] --> RL2["L2 特征/去水"]
        RL2 --> RL3["L3 决策（三路分流）"] --> RL4["L4 经济学算法栈"]
        RL4 --> RL5["L5 执行"]
        RL5 --> RL6["L6 反馈校准"]
    end

    subgraph I["本原型实现映射"]
        direction TB
        IL1["odds-collector + browser-scraper<br/>✅ 双路径（逆向 + 浏览器）"]
        IL1B["microstructure<br/>🔬 跳动/恢复/漂移"]
        IL2["valuation-core.devig<br/>✅ 五法交叉"]
        IL3["rule-engine + ai-analyzer<br/>✅ 规则关键路径 · LLM 旁路"]
        IL4["valuation-core.sizing<br/>✅ 分数/多元 Kelly"]
        IL5["❌ 不实现"]
        IL6["calibration<br/>✅ Brier/ECE/CLV"]
    end

    RL1 -.-> IL1
    RL1B -.-> IL1B
    RL2 -.-> IL2
    RL3 -.-> IL3
    RL4 -.-> IL4
    RL5 -.->|"刻意剥离"| IL5
    RL6 -.-> IL6
```

---

## 3. 全链路数据流

### 3.1 采集到定价的时序

```mermaid
sequenceDiagram
    autonumber
    participant U as 🖥️ Web 控制台
    participant A as analytics-api
    participant C as odds-collector
    participant S as browser-scraper
    participant T as 目标站点
    participant V as valuation-core
    participant R as redis
    participant L as LLM API

    U->>A: GET /api/v1/matches?date=2026-06-01
    A->>R: 查询赛事快照缓存
    alt 缓存命中
        R-->>A: 赛事列表
    else 缓存未命中
        A->>C: 触发采集任务
        C->>S: POST /scrape {url, config}
        S->>T: 无头 Chrome 渲染 / 复用内部端点
        T-->>S: HTML / JSON
        S-->>C: {title, content, meta, links}
        C->>C: 解析 → 归一化 → 赛事ID对齐
        C->>R: SETEX 快照 (TTL)
        C->>C: 追加不可变快照 output/
        R-->>A: 赛事列表
    end
    A-->>U: 200 {matches:[...]}

    U->>A: GET /api/v1/fair/{match_id}
    A->>V: devig(odds, method="auto")
    Note over V: 五法交叉：Proportional /<br/>Additive / Power / Odds-ratio / Shin
    V->>V: 计算 booksum、水钱 m、公平概率
    V->>V: 方法间 L1 偏差校验（超阈值告警）
    V-->>A: {fair, margin, method_spread, shin_z}
    A-->>U: 200 {p_fair, margin, edge}

    Note over U,L: —— 以下为慢通路，异步且不阻塞 ——
    A->>L: POST /chat/completions（伤停/新闻抽取）
    L-->>A: 结构化特征
    A->>R: 异步注入特征库
```

### 3.2 去水与优势估计的判定流（报告 §8.1–8.5）

```mermaid
flowchart TD
    START(["赔率快照 o₁…oₙ"]) --> B["计算 booksum B = Σ(1/oᵢ)<br/>水钱 m = B − 1"]
    B --> Q1{"m 是否在<br/>合理区间？"}

    Q1 -->|"m < 0"| ERR1["❌ 数据异常<br/>（套利或解析错误）"]
    Q1 -->|"m > 25%"| ERR2["⚠️ 极端水钱<br/>长尾/低流动性，标记"]
    Q1 -->|"0 ≤ m ≤ 25%"| DEVIG["五法交叉去水"]

    DEVIG --> P1["Proportional<br/>p = q/B"]
    DEVIG --> P2["Shin<br/>解 z 使 Σp=1"]
    DEVIG --> P3["Power<br/>解 k 使 Σq^k=1"]

    P1 --> SPREAD{"方法间<br/>L1 偏差 > 1pp？"}
    P2 --> SPREAD
    P3 --> SPREAD

    SPREAD -->|"是"| WARN["⚠️ 标记为高分歧<br/>报告中实测最大 8.0pp<br/>此时 edge 符号不可信"]
    SPREAD -->|"否"| FAIR["采用 Shin 为基准<br/>（内生 FL bias 修正）"]

    FAIR --> EDGE["edge = p_model × o − 1"]
    WARN --> BLOCK["❌ 禁止据此计算 edge"]

    EDGE --> Q2{"edge > 0？"}
    Q2 -->|"否"| SKIP["跳过（无优势）"]
    Q2 -->|"是"| SHRINK["贝叶斯收缩<br/>p̂ = w·p_hat + (1−w)·p_prior"]

    SHRINK --> SIZE["分数/多元 Kelly<br/>f* = Σ⁻¹μ × λ"]
    SIZE --> Q3{"q_fill 估计<br/>是否过低？"}
    Q3 -->|"是"| DROP["执行过滤：放弃"]
    Q3 -->|"否"| OUT(["输出研究结论<br/>（不执行投注）"])

    classDef bad fill:#fdecea,stroke:#c0392b,stroke-width:2px,color:#12212e
    classDef warn fill:#fdf6e6,stroke:#8a5a00,stroke-width:1px,color:#12212e
    classDef good fill:#eef9f2,stroke:#27ae60,stroke-width:2px,color:#12212e
    class ERR1,BLOCK bad
    class ERR2,WARN warn
    class FAIR,OUT good
```

---

## 4. 任务状态机

### 4.1 采集任务生命周期

```mermaid
stateDiagram-v2
    [*] --> 待调度: API 请求 / 定时触发
    待调度 --> 端点探测: 检查目标可达性
    待调度 --> 已取消: 请求方撤回

    端点探测 --> 逆向优先: 探测到内部端点
    端点探测 --> 浏览器兜底: 未探测到 / 逆向失败
    端点探测 --> 已失败: 全部端点不可达

    逆向优先 --> 采集成功: 拿到结构化 JSON
    逆向优先 --> 浏览器兜底: 协议变更 / 字段缺失

    浏览器兜底 --> 采集成功: 渲染后解析成功
    浏览器兜底 --> 验证页拦截: 触发人机校验
    浏览器兜底 --> 已失败: 超时 / 渲染异常

    验证页拦截 --> 人工介入: 需人工处理
    人工介入 --> 浏览器兜底: 获得凭据后重试
    人工介入 --> 已失败: 超时放弃

    采集成功 --> 归一化: 赛事ID / 时区 / 字段映射
    归一化 --> 落库: 写 redis + 不可变快照
    归一化 --> 已失败: 校验不通过

    落库 --> [*]
    已失败 --> [*]
    已取消 --> [*]

    note right of 逆向优先
        报告 §3.2 路径 B：
        接入层级 4 · 延迟 4
        免浏览器，工程推荐主路径
    end note

    note right of 浏览器兜底
        报告 §3.2 路径 C：
        完整性 5（最高）但延迟仅 2
        且 CPU 开销大（allusion 警告）
    end note

    note right of 落库
        不可变快照（snuper 模式）
        报告 §12.1：还原"过程"而非"状态"
    end note
```

### 4.2 赔率快照的状态语义

```mermaid
stateDiagram-v2
    [*] --> 有效
    有效 --> 停盘: 关键事件 suspend
    停盘 --> 有效: 事件结束恢复
    有效 --> 已下架: 赛事结束/撤盘
    停盘 --> 已下架: 长时间未恢复

    有效 --> 陈旧: 超过刷新窗口未见更新
    陈旧 --> 有效: 收到新推送
    陈旧 --> 隔离: 持续陈旧超阈值

    note right of 停盘
        报告 §5.3：
        此时读到的价格是冻结值，
        必须排除在微结构信号之外
    end note

    note right of 陈旧
        报告 §7.1：
        陈旧价格是"未定价"与"报价撤回"
        的混合，不可用于 edge 计算
    end note
```

---

## 5. 服务清单与职责边界

| 服务 | 容器名 | 端口 | 状态 | 职责 | 报告依据 |
| --- | --- | --- | --- | --- | --- |
| `redis` | ai_football_redis | 6379 | ✅ **已验证** | 会话、快照缓存、任务状态 | — |
| `browser-scraper` | ai_football_browser_scraper | 8080 | ✅ **已验证** | 无头 Chrome 渲染，`/health` `/scrape` `/scrape/batch` | §3.2 路径 C |
| `ai-analyzer` | ai_football_analyzer | 8000 | ✅ **已构建** | 分析编排、LLM 调用（慢通路） | §6.3 |
| `odds-collector` | *(库模块)* | — | ✅ **已实现** | L1 归一化 + L1′ 微结构 + 采集编排 | §3.2 A/B 路径 |
| `valuation-core` | *(库模块)* | — | ✅ **已实现** | L2 去水 + L4 经济学算法栈 | §8.1–8.8 |
| `analytics-api` | ai_football_analytics_api | 8000 | ✅ **已实现** | REST 接入层（11 端点），供控制台调用 | §2 |
| `web-console` | ai_football_web_console | 3000 | ✅ **已实现** | B/S 的 Browser 端（nginx + 静态） | §7 |

### 5.1 职责边界（AGENTS.md §3.2 强制）

```mermaid
flowchart LR
    subgraph OK["✅ 允许的调用方向"]
        direction LR
        A1["analytics-api"] -->|"只做编解码/校验/装配"| A2["valuation-core"]
        A2 -->|"纯函数式计算<br/>无 I/O"| A3["(返回结果)"]
        A4["odds-collector"] -->|"HTTP"| A5["browser-scraper"]
    end

    subgraph NO["❌ 禁止的模式"]
        direction LR
        B1["analytics-api"] -->|"❌ 直接写业务 SQL"| B2["(数据库)"]
        B3["browser-scraper"] -->|"❌ 内嵌分析逻辑"| B4["(LLM)"]
        B5["odds-collector"] -->|"❌ 直接 import selenium"| B6["(浏览器)"]
    end

    classDef ok fill:#eef9f2,stroke:#27ae60,stroke-width:2px,color:#12212e
    classDef no fill:#fdecea,stroke:#c0392b,stroke-width:2px,color:#12212e
    class A1,A2,A3,A4,A5 ok
    class B1,B2,B3,B4,B5,B6 no
```

> **关键约束**：`browser-scraper` 是**唯一**允许 `import selenium` 的容器；
> `odds-collector` 必须通过 `browser_scraper_client.py` 走 HTTP 调用。
> 这条边界来自既有代码结构（`browser_scraper_client.py` / `browser_scraper_service.py` 的 C/S 拆分）。

---

## 6. API 契约

> 所有端点均在 `/api/v1` 下，返回 JSON。**原型设计阶段，尚未实现。**

### 6.1 端点定义

| 方法 | 路径 | 说明 | 报告依据 |
| --- | --- | --- | --- |
| `GET` | `/health` | 服务与依赖健康状态 | — |
| `GET` | `/matches` | 赛事列表（支持日期/联赛/关键词过滤） | — |
| `GET` | `/matches/{id}` | 单场详情（基本信息 + 赔率 + 玩法） | — |
| `GET` | `/odds/{id}` | 赔率快照（含状态：有效/停盘/陈旧） | §4.2 |
| `GET` | `/fair/{id}` | **去水后公平概率**（五法交叉 + 分歧告警） | §8.1 |
| `GET` | `/edge/{id}` | 优势估计（含收缩后概率） | §8.2 / §8.4 |
| `GET` | `/microstructure/{id}` | 微结构信号（跳动频率/恢复时间/漂移速率） | §5.1 |
| `POST` | `/portfolio` | 多元 Kelly 仓位建议（含相关性约束） | §8.5 |
| `GET` | `/calibration` | 校准报告（Brier / ECE / CLV / 对数增长率 / 最大回撤） | §8.8 |
| `POST` | `/collect` | 触发采集任务（异步，返回 task_id） | §4.1 |
| `GET` | `/tasks/{task_id}` | 采集任务状态查询 | §4.1 |
| `GET` | `/markets` | **玩法目录**（支持的 9 种玩法及其结果空间） | §8.1 |
| `GET` | `/markets/{id}` | **某场全部玩法统计**（各玩法水钱/EV/分歧） | §8.1 |
| `GET` | `/markets/{id}/{market}` | 单玩法逐结果明细（赔率/隐含/公平概率/公平赔率） | §8.1 |
| `GET` | `/model/{id}?market=` | **Poisson 模型概率**（独立于市场的 p_model） | §7.1 / §8.2 |
| `GET` | `/consistency/{id}` | 跨玩法边际一致性校验（数据错误 vs 套利） | §8.4 |

> **多玩法端点说明**：一场比赛的玩法远不止「胜平负」。体彩官方单场可售
> 5 种玩法（HAD 胜平负 / HHAD 让球 / TTG 总进球 / CRS 比分 / HAFU 半全场），
> 标准赔率源另有 AH / OU / BTTS / DC。本系统对**每种玩法独立去水、独立算 EV**，
> 因为不同玩法的水钱差异极大（实测 HAD 12.97% vs CRS 29.73%），
> 对应 EV 从 −11.48% 恶化到 −22.92%。详见 §6.3。

### 6.2 响应示例（基于真实数据）

以下数值来自仓库内 [`output/场次*.json`](output/) 的 **15 场真实赔率**实测：

```json
{
  "match_id": "2001",
  "league": "日职",
  "teams": "鹿岛鹿角 vs 大阪樱花",
  "odds": { "home": 2.13, "draw": 3.45, "away": 2.70 },
  "implied_raw": { "home": 0.4695, "draw": 0.2899, "away": 0.3704 },
  "booksum": 1.1297,
  "margin": 0.1297,
  "fair": {
    "method": "shin",
    "probabilities": { "home": 0.4270, "draw": 0.2652, "away": 0.3078 },
    "shin_z": 0.0651
  },
  "method_spread_pp": 1.554,
  "spread_warning": false,
  "ev_if_fair": -0.1148,
  "execution": {
    "auto_betting": "NOT_SUPPORTED",
    "reason": "REPORT §5.3 — 成交裁定权在对手方；§9.3 触发条件为注单质量"
  }
}
```

> **实测统计（15 场样本，2025-09-23 批次）**：
> - 水钱：均值 **12.94%**、中位数 12.94%、区间 [12.85%, 12.99%]
> - 按报告公式 `EV = −m/(1+m)` → 单注期望 **−11.45%**
> - 去水方法间 L1 偏差：均值 **3.54 pp**、**最大 8.01 pp**（AC米兰 vs 莱切）
> - Shin `z` 估计：0.064–0.071（与报告 §8.1 的示例盘口 0.0157 同量级）
>
> ⚠️ **±1 pp 的典型偏差意味着**：在这些盘口上，**去水方法的选择本身就会翻转 `edge` 的符号**。
> 这正是报告 §8.1 的核心论点，在本仓库的真实数据上得到了复现。

### 6.3 多玩法统计（每场比赛的全部玩法）

**动机**：一场比赛有很多种投注玩法，只算「胜平负」会严重低估亏损速度。

**核心机制**：`core/markets.py` 提供数据源无关的玩法目录，把每种玩法的
结果空间显式描述出来，使去水/EV/校准对玩法**完全通用**。

```text
                      ┌──────────────────────────────┐
                      │   比分分布 M[i][j]           │
                      │   P(主 i 球, 客 j 球)        │
                      │   由 λ_home / λ_away 生成    │
                      └───────────────┬──────────────┘
                                      │ 解析导出
        ┌──────────┬──────────┬───────┴────┬──────────┬──────────┐
        ▼          ▼          ▼            ▼          ▼          ▼
     HAD 胜平负  HHAD 让球   TTG 总进球  CRS 比分   HAFU 半全场  OU/BTTS/DC
     3 结果      3 结果      8 结果      31 结果     9 结果      2–3 结果
        │          │          │            │          │          │
        └──────────┴──────────┴─────┬──────┴──────────┴──────────┘
                                    ▼
                         每种玩法独立去水 → 独立 EV
                                    ▼
                        跨玩法边际一致性校验（§8.4）
```

**支持的玩法（9 种）**：

| 代码 | 名称 | 结果数 | 结果空间 | 说明 |
| --- | --- | --- | --- | --- |
| `HAD` | 胜平负 | 3 | 完备划分 | 官方 poolCode HAD |
| `HHAD(-1)` | 让球胜平负 | 3 | 完备划分 | 含让球盘口，主队让 1 球 |
| `TTG` | 总进球 | 8 | 完备划分 | 0–6 精确，`7+` 为聚合桶 |
| `CRS` | 比分 | 31 | 完备划分 | 具体比分 + 胜/平/负「其他」桶 |
| `HAFU` | 半全场 | 9 | 完备划分 | 标签格式「半场/全场」 |
| `AH(-0.5)` | 亚洲让球 | 2 | 完备划分 | 走盘按各半处理 |
| `OU(2.5)` | 大小球 | 2 | 完备划分 | 走盘按退还处理 |
| `BTTS` | 双方进球 | 2 | 完备划分 | — |
| `DC` | 双重机会 | 3 | **结果重叠** | 主胜同属 `1X` 与 `12`，**和为 2** |

> ⚠️ **结果空间是必须显式建模的概念**。双重机会的三个结果**互相重叠**
> （主胜同时属于 `1X` 和 `12`），概率之和为 2。若误当作完备划分去归一化，
> 每个概率会被**静默砍半**（0.705 → 0.352），不抛任何异常。
> 本模块用 `ResultSpace.OVERLAPPING` 显式区分，并有回归测试守护。

**实测：不同玩法的水钱与 EV 差异**

以下数值来自同一场比赛（2001 鹿岛鹿角 vs 大阪樱花）的合成赔率样本，
经 `parse_pooled_odds` → 独立去水计算：

| 玩法 | booksum | 水钱 m | 公平定价 EV = −m/(1+m) |
| --- | --- | --- | --- |
| `HAD` 胜平负 | 1.1297 | **12.97%** | **−11.48%** |
| `HHAD(-1)` 让球 | 1.1299 | 12.99% | −11.50% |
| `HAFU` 半全场 | 1.1959 | 19.59% | −16.38% |
| `TTG` 总进球 | 1.2188 | 21.88% | −17.95% |
| `CRS` 比分 | 1.2973 | **29.73%** | **−22.92%** |

> **结论**：水钱跨度达 **16.76 个百分点**，EV 从 −11.48% 恶化到 −22.92%。
> 这直接印证报告 §9.1——**越冷门的玩法水钱越重，亏得越快**。
> 「多玩法统计」的价值正在于此：不统计就看不到这个差异。

**模型概率 vs 市场定价（真实 edge 的唯一来源）**

`/model/{id}` 用独立泊松比分模型给出 `p_model`，与去水后的市场概率对比：

| 结果 | 赔率 | 盈亏平衡 p = 1/o | 模型 p | 市场 p | 模型 − 市场 | 模型 edge = p·o − 1 |
| --- | --- | --- | --- | --- | --- | --- |
| home | 2.13 | 46.95% | 38.27% | 42.34% | −4.06% | **−18.48%** |
| draw | 3.45 | 28.99% | 29.79% | 24.94% | +4.85% | **+2.79%** |
| away | 2.70 | 37.04% | 31.93% | 32.73% | −0.79% | **−13.78%** |

> ⚠️ **诚实性警告**：本场 3 个结果中有 1 个模型 edge 为正（draw +2.79%）。
> 在**没有独立信息优势**的前提下，这**通常是模型噪声，而非真实优势**
> （报告 §7.1）。朴素的独立泊松模型在 17 场样本上给出 **44.4% 的腿为正期望**，
> 均值 +7.89%、范围 −59% ~ +129%——这个分布宽度本身就说明是噪声。
> **任何正 edge 都必须先经 L6 校准（Brier/CLV）验证，否则不得当作信号。**

**模型局限（`/model` 响应中显式返回）**

- 独立泊松假设：未建模进球相关性（Dixon-Coles ρ 修正未实现）
- 未建模红牌/伤停/轮换的实时影响
- 样本量小时（场均数据来自近期 10 场）偏差较大

---

### 6.4 数据源边界（重要）

**本项目的数据来源是中国体育彩票官方 API V2**（`webapi.sporttery.cn`），
即合法的国家队竞猜数据源，落库于 `output/场次*.json`。

**明确排除：乐鱼体育 / 开云体育不作为数据源。**

| 项目 | 说明 |
| --- | --- |
| 性质 | 乐鱼、开云是**无牌离岸博彩站点**，在中国境内属非法博彩 |
| 为何不接 | 「获取其数据」实际意味着绕过反爬/鉴权、伪造 token、逆向私有 API，属未授权访问 |
| 本项目的做法 | 采集层设计为**数据源无关**——任何合法授权的赔率源都可归一化后接入 |
| 如需接入 | 请使用持牌赔率供应商（如 the-odds-api 等商业 API），或官方体彩接口 |

> 这**不是**技术可行性问题，而是**数据来源合法性**问题。
> 报告 §3 已系统评估过采集路径的**技术**可行性（含逆向 GraphQL 路径），
> 但技术可行不等于应当实施。本系统只做**技术与经济学**研究。

**已有数据缺口（诚实披露）**

`output/场次*.json` 仅落库了 `HAD` 与 `HHAD` 两种玩法的赔率，
`TTG` / `CRS` / `HAFU` 虽在 `玩法信息.可用玩法` 中标记为 `Selling`（可售），
**但赔率字段并未保存**。因此：

- `/markets/{id}` 对既有数据只返回 2 种玩法（这是真实的，不是 bug）
- `/markets/{id}/CRS` 会返回 **404 并明确说明原因**，而非静默返回空
- 采集器（`collector.normalizer.parse_pooled_odds`）**已支持全部 5 种玩法**，
  待官方赔率接口恢复可访问（当前返回 WAF 拦截 HTTP 567）后即可补齐

---

## 7. Web 控制台

> B/S 架构的 Browser 端。**已实现**：`web/console/index.html`（零 `innerHTML`，
> 全部经 `createElement` / `textContent` 构建，从结构上消除 XSS 注入面）。
> 本节既是交互规范（AGENTS.md §5.2.3），也对应已上线的实现。

### 7.0 视觉风格

采用**深色体育数据终端**风格：深底 + 高对比等宽数字 + 状态色分级
（绿=正期望 / 红=负期望 / 琥珀=告警）。这是体育赔率类界面的通行视觉语言：
暗背景降低长时间盯盘的疲劳，等宽数字（`font-variant-numeric: tabular-nums`）
让赔率便于纵向比读。

主题通过 CSS 变量集中管理（`--bg` / `--card` / `--pos` / `--neg` / `--amb` …），
可整体切换明暗。

> **边界声明**：此处**仅借鉴通用视觉风格**，未复制任何博彩站点的
> 素材、商标、图标或代码。乐鱼、开云是无牌离岸博彩，本项目不与其
> 发生任何数据或品牌关联（见 §6.4）。

### 7.1 布局线框图

```text
┌──────────────────────────────────────────────────────────────────────────────────────┐
│  ⚽ ai_football   [赛事看板] [定价对比] [校准报告]        仅研究用途·不提供投注执行  │
├────────────┬─────────────────────────────────────────────────────────────────────────┤
│            │  ┌─ 筛选栏 ─────────────────────────────────────────────────────────┐  │
│  📋 导航    │  │ 📅 日期 [2025-09-23 ▾]   🏆 联赛 [全部 ▾]   ⚙️ 去水 [Shin ▾]  │  │
│            │  │ ☐ 仅显示有水钱异常   ☐ 仅显示高分歧(>1pp)      [🔄 刷新]         │  │
│  • 赛事看板 │  └──────────────────────────────────────────────────────────────────┘  │
│  • 赔率曲线 │  ┌─ 数据卡片 ×4 ───────────────────────────────────────────────────┐  │
│  • 定价对比 │  │ 赛事数      │ 均值水钱    │ 期望 EV      │ 高分歧场次           │  │
│  • 校准报告 │  │   15        │  12.94%     │  −11.45%     │  2 / 15              │  │
│  • 采集任务 │  └──────────────────────────────────────────────────────────────────┘  │
│  • 系统状态 │  ┌─ 赛事数据表 ────────────────────────────────────────────────────┐  │
│            │  │ 场次  联赛   对阵                 赔率(主/平/客)  水钱  偏差  状态│  │
│            │  │ 2001  日职   鹿岛鹿角 vs 大阪樱花  2.13/3.45/2.70  12.97% 1.55  ✅│  │
│            │  │ 2002  日职   清水鼓动 vs 浦和红钻  2.72/3.25/2.20  12.99% 1.31  ✅│  │
│            │  │ 3011  意杯   AC米兰 vs 莱切        1.17/5.60/10.50 12.85% 8.01  ⚠️│  │
│            │  │  ...                                                             │  │
│            │  └──────────────────────────────────────────────────────────────────┘  │
│            │  ┌─ 详情面板 ① 去水方法对比 ───────────────────────────────────────┐  │
│            │  │ 结果   赔率   隐含    比例法   加法法   幂法   赔率比   Shin    edge│  │
│            │  │ 主胜   2.13  46.95%  41.56%  42.62%  42.63%  42.18%  42.34%    —  │  │
│            │  │ 平局   3.45  28.99%  25.66%  24.66%  24.75%  25.17%  24.94%    —  │  │
│            │  │ 客胜   2.70  37.04%  32.78%  32.71%  32.63%  32.65%  32.73%    —  │  │
│            │  │ ⚠️ 方法间 L1 偏差 2.14 pp —— 超过 1 pp 阈值，本行 edge 已禁用     │  │
│            │  └──────────────────────────────────────────────────────────────────┘  │
│            │  ┌─ 详情面板 ② 各玩法统计（一场比赛有多种玩法）─────────────────────┐  │
│            │  │ 玩法        结果数  booksum  水钱 m   公平定价 EV  方法间偏差 状态│  │
│            │  │ 1X2 胜平负     3    1.1297  12.97%   −11.48%     2.140 pp  ⚠️ 高分歧│
│            │  │ AH 让球        3    1.1299  12.99%   −11.50%     6.746 pp  ⚠️ 高分歧│
│            │  │ CRS 比分      31    1.2973  29.73%   −22.92%    24.848 pp  ⚠️ 高分歧│
│            │  │ TTG 总进球     8    1.2188  21.88%   −17.95%    12.494 pp  ⚠️ 高分歧│
│            │  │ HAFU 半全场    9    1.1959  19.59%   −16.38%    10.929 pp  ⚠️ 高分歧│
│            │  │ 水钱跨度 16.76 pp —— 冷门玩法亏得更快（报告 §9.1）                │  │
│            │  └──────────────────────────────────────────────────────────────────┘  │
│            │  ┌─ 详情面板 ③ 模型概率 vs 市场定价（真实 edge 的唯一来源）────────┐  │
│            │  │ λ_home 1.117  λ_away 0.993  合计 2.110   市场水钱 12.97%         │  │
│            │  │ 结果  赔率  盈亏平衡p  模型p   市场p  模型−市场  模型 edge       │  │
│            │  │ home  2.13   46.95%   38.27%  42.34%   −4.06%    −18.48%        │  │
│            │  │ draw  3.45   28.99%   29.79%  24.94%   +4.85%     +2.79%        │  │
│            │  │ away  2.70   37.04%   31.93%  32.73%   −0.79%    −13.78%        │  │
│            │  │ ⚠️ 3 个结果中 1 个为正 —— 无信息优势时通常是模型噪声而非真实优势  │  │
│            │  │    必须用 L6 校准（Brier/CLV）验证（报告 §7.1）                   │  │
│            │  └──────────────────────────────────────────────────────────────────┘  │
└────────────┴─────────────────────────────────────────────────────────────────────────┘
```

### 7.2 五大界面状态定义（AGENTS.md §5.2.3 强制）

| 状态 | 触发条件 | 视觉规范 | 交互与文案 |
| --- | --- | --- | --- |
| **`Default`** | 数据正常返回 | 卡片 + 表格完整渲染；水钱 ≤ 阈值用绿色，超阈值橙色 | 表格行可点击展开详情；支持列排序 |
| **`Loading`** | 请求进行中 | 卡片区骨架屏（4 个灰块）；表格区 8 行骨架行；顶部细进度条 | 骨架屏保留表格列头，避免布局跳动；禁用重复提交 |
| **`Empty`** | 查询无结果 | 居中插画 + 主文案「该日期暂无赛事数据」+ 副文案说明可能原因 | 行动倡导按钮：`[🔄 换一个日期]` `[📥 手动触发采集]` |
| **`Error`** | 网络失败 / 5xx / 解析异常 | 顶部 Banner（红色左边框）：错误码 + 简短原因 + `[重试]` | 区分三类：`网络不可达`／`采集任务失败`／`数据格式异常`，各自给不同引导 |
| **`Edge-Case`** | 长文本 / 极值 / 异常数据 | 队名超 20 字省略号 + `title` 悬浮全文；赔率 > 99 保留 2 位；水钱 > 25% 标红加 `⚠️`；`booksum < 1` 标「疑似套利/解析错误」 | 高分歧（>1pp）行整体降饱和 + 角标，且**禁用其 edge 展示**（报告 §8.1） |

### 7.3 关键交互约束（源自报告结论）

| 约束 | 设计体现 | 报告依据 |
| --- | --- | --- |
| **不展示"买入"按钮** | 页面任何位置均无下单入口 | §5.3、§13.2 |
| **高分歧场次禁用 edge** | `method_spread > 1pp` 时 `edge` 列显示 `—` 并提示原因 | §8.1 |
| **停盘价格不参与信号** | 状态列显示 `⏸ 停盘`，该行从微结构统计中排除 | §5.3 |
| **陈旧价格显式标注** | 超刷新窗口显示 `🕐 陈旧(Ns)` | §7.1 |
| **湿示负期望** | 顶部卡片固定展示当前 EV，不做美化 | §9.1 |
| **仓位建议标注不确定性** | Kelly 输出附 `λ` 与收缩权重 `w` | §8.4 |

---

## 8. 目录结构

```text
ai_football/
├── AGENTS.md                      # 工程宪章（运行约束·架构红线·DoD）
├── README.md                      # 本文件
├── Dockerfile                     # ai-analyzer 镜像（已补齐，此前缺失）
├── docker/
│   ├── docker-compose.yml         # B/S 编排（redis + browser-scraper + ai-analyzer）
│   ├── Dockerfile.browser         # Chrome 沙盒镜像
│   └── requirements-browser.txt   # 容器专用依赖
├── requirements.txt
├── config.py                      # ⚠️ 唯一配置源（含明文 token，见 §12）
│
├── browser_scraper_service.py     # L1 采集服务（容器内，Flask :8080）
├── browser_scraper_client.py      # L1 采集客户端（宿主侧 HTTP 封装）
├── main.py                        # 采集编排（搜索 → 抓取 → 落库）
├── enhanced_analyzer.py           # L3/L6 分析与校准
├── match_generator.py             # 赛事数据获取与中文字段映射
├── search_tools/                  # 检索器（base_searcher 为基类）
│   ├── base_searcher.py
│   ├── sogou_searcher.py
│   └── sportsdata_searcher.py
│
├── reports/                       # 📊 研究报告（AGENTS.md §5.1 约定结构）
│   └── leyu-kaiyun-odds-bot-feasibility/
│       ├── REPORT.md              # 主报告（17 图 / 53 表 / 5 Mermaid）
│       ├── REPORT.html            # 单文件 HTML（含 Mermaid 运行时）
│       ├── images/                # 17 张图表
│       ├── data/                  # 12 个 CSV（可复核）
│       └── scripts/               # 可复现脚本
│
├── web/                           # ⚠️ 站点采集器（含重复副本，见 §12）
│   ├── 澳客爬虫/ 球迷屋爬虫/ 虎扑爬虫/ DS爬虫/
│   └── enhanced_analyzer.py       # ← 与根目录重复
│
└── output/                        # 运行产物（已 gitignore）
    ├── 场次*.json                 # 赛事 + 赔率数据
    ├── articles/                  # 文章语料
    └── analysis/                  # 分析结果
```

---

## 9. 快速开始

### 9.1 前置要求

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| Docker | ≥ 24 | 已验证 29.8.1 |
| Docker Compose | V2 | 需支持 `profiles` |
| Python | 3.12 | 宿主侧脚本与报告复现 |

> ⚠️ **宿主机无 pip，且缺少 `selenium`/`flask`/`redis`/`pytest`**（AGENTS.md §0）。
> 业务代码一律在容器内运行，**不要在宿主机 `pip install`**。

### 9.2 启动全栈（推荐：一键脚本）

```bash
./scripts/up.sh            # 自动避让被占用端口并启动
./scripts/up.sh --dry-run  # 只看将使用哪些端口，不启动
./scripts/up.sh --down     # 仅停止本项目
```

**脚本的核心保证：绝不触碰宿主机上其他项目的服务。**

| 场景 | 行为 |
| --- | --- |
| 端口被**其他项目**占用 | 只为本项目挑选空闲端口（如 6379→6380、8080→8081） |
| 端口已被**本项目**容器占用 | **复用**（保证脚本幂等，重复运行端口不漂移） |
| 其他项目的容器 | **不停止、不修改、不重启** |

> 这是硬性约束：很多开发机同时跑着多个 compose 项目，
> 端口冲突时停掉别人的服务是不可接受的。

### 9.3 启动全栈（手工方式）

```bash
# 端口冲突时用环境变量覆盖（默认 6379 / 8080 / 8000 / 3000）
export REDIS_PORT=16380
export SCRAPER_PORT=18080
export ANALYTICS_PORT=18000
export CONSOLE_PORT=13000

docker compose -f docker/docker-compose.yml up -d --build
docker compose -f docker/docker-compose.yml ps
curl -s http://localhost:18000/health
```

**已验证输出**（4 容器全 healthy）：

```text
NAME                        STATUS                   PORTS
ai_football_analytics_api   Up (healthy)             0.0.0.0:18000->8000/tcp
ai_football_browser_scraper Up (healthy)             0.0.0.0:18080->8080/tcp
ai_football_redis           Up                       127.0.0.1:16380->6379/tcp
ai_football_web_console     Up                       0.0.0.0:13000->80/tcp
```

**已验证输出**：

```text
NAME                          STATUS                    PORTS
ai_football_browser_scraper   Up (healthy)              0.0.0.0:18080->8080/tcp
ai_football_redis             Up                        127.0.0.1:16379->6379/tcp
```

### 9.4 抓取接口

```bash
curl -s -X POST http://localhost:18080/scrape \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","config":{"enable_javascript":false}}' \
  | python3 -m json.tool
```

**已验证响应字段**：`success` `url` `title` `content` `meta` `links` `images` `execution_time` `timestamp`

### 9.5 运行分析器（一次性任务）

`ai-analyzer` 使用 `profiles` 隔离，不随 `up` 启动（分析器是一次性任务而非长驻服务）：

```bash
# 需要 config.py 提供 API Token
docker compose -f docker/docker-compose.yml run --rm ai-analyzer

# 或传参
docker compose -f docker/docker-compose.yml run --rm ai-analyzer \
  python3 enhanced_analyzer.py --resume --workers 20 -v
```

### 9.6 复现研究报告

```bash
python3 reports/leyu-kaiyun-odds-bot-feasibility/scripts/make_figures.py
# → 17 图 + 12 CSV

python3 reports/leyu-kaiyun-odds-bot-feasibility/scripts/md_to_html.py \
  reports/leyu-kaiyun-odds-bot-feasibility/REPORT.md \
  reports/leyu-kaiyun-odds-bot-feasibility/REPORT.html
```

### 9.7 验收自检（AGENTS.md §6 DoD）

```bash
# 语法检查
python3 -m py_compile *.py search_tools/*.py web/*/main.py reports/*/scripts/*.py

# 容器内依赖与模块加载
docker run --rm -v "$PWD/config.py:/app/config.py:ro" ai_football-ai-analyzer \
  python3 -c "import config, match_generator, browser_scraper_client, enhanced_analyzer; print('OK')"

# 卫生检查：禁止产物入库
git status --short | grep -vE '^D ' | grep -E '__pycache__|\.pyc$|output/|返回信息\.txt' \
  && echo "❌ 产物待入库" || echo "PASS"
```

---

## 10. 研究报告关键结论

本系统原型的设计依据来自 [完整报告](reports/leyu-kaiyun-odds-bot-feasibility/REPORT.md)。核心结论摘录：

### 10.1 架构是对的，但优化的是负期望系统

| 维度 | 结论 |
| --- | --- |
| **技术可行性** | 分层设计正确，可从「延迟预算 × 调用频率 × 单次成本」严格推导三路分流 |
| **经济学可行性** | ❌ 在盘水钱均值 11.4% → `EV = −10.23%`；打平需**相对优于庄家定价模型 11.4%**，公开数据可得 **0–2%** |
| **关键路径** | 总计 29.0s，**属于你的只有 1.8s**；LLM 不应进关键路径（+20% 延迟，定价贡献 ≈ 0） |
| **成交裁定** | 5–20s 接受延迟 + 改价 + 拒单 + 基于 CLV 的降限额 → **由对手方单方面裁定** |
| **成本结构** | $35.3/天，**模型调用仅占 20%**，人力与采集基础设施占 80% |

### 10.2 蒙特卡洛结果（2 万次 / 90 天）

| 情形 | 90 日余额中位数 | 盈利概率 |
| --- | ---: | ---: |
| A · 无优势（水钱 11.4%） | $3,631 | **0.8%** |
| B · +2% 优势，不计成本 | $11,259 | 61.4% |
| D · +2% 优势，扣 $35/天 成本 | $7,655 | **45.4%** |
| C · +2% 优势 + 25% 平台风险 | $9,263 | **29.2%** |

### 10.3 本仓库真实数据的复现验证

在 15 场真实赔率上复现了报告 §8.1 的核心论点：

```text
水钱：均值 12.94%（[12.85%, 12.99%]）
预期 EV（按报告公式）：−11.45%
去水方法间 L1 偏差：均值 3.54 pp，最大 8.01 pp（AC米兰 vs 莱切）
```

> **±1 pp 的偏差足以翻转 `edge` 的符号** —— 因此本系统原型把「方法间分歧」作为**一等公民**
> 纳入 API 契约（`method_spread` + `spread_warning`）与 UI 状态（高分歧行禁用 edge）。

### 10.4 可迁移的工程产出

报告中列出的可迁移产出，在本原型中全部落地为模块：

| 产出 | 原型落点 |
| --- | --- |
| 多路径采集架构（长连/逆向/浏览器/聚合/API） | `odds-collector` + `browser-scraper` |
| 不可变快照 + 时序事件模型 | `output/` 卷 + redis 双写 |
| 三路分流决策架构 | `rule-engine` + `ai-analyzer` |
| 去水方法体系与交叉校准 | `valuation-core.devig`（五法 + 分歧告警） |
| 经济学算法栈（收缩/多元 Kelly/执行过滤） | `valuation-core.sizing` |
| 校准评估闭环（Brier/ECE/CLV） | `ai-analyzer.calibration` |

---

## 11. 技术选型对比矩阵

按 AGENTS.md §5.2.2 要求，对采集路径做 **8 方案 × 9 维度**评估（完整版见[报告 §12.5](reports/leyu-kaiyun-odds-bot-feasibility/REPORT.md)）。

| 方案 | 接入层级 | 延迟表现 | 盘口完整性 | 时间分辨率 | 稳定性 | 维护成本 | 覆盖率 | 自研成本 | 依赖风险 |
| --- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **A · 前端长连直读 WS/SSE** | 5 | **5** | 3 | **5** | 2 | 2 | 5 | 3 | **1** |
| **B · XHR/GraphQL 逆向** | 4 | 4 | 4 | 4 | 3 | 3 | 5 | 4 | 3 |
| **C · 浏览器自动化** ✅已实现 | 3 | 2 | **5** | 3 | 3 | 2 | 5 | 3 | 3 |
| **D · 第三方聚合站** | 2 | **1** | 4 | **1** | 4 | **5** | 4 | **5** | 4 |
| **E · 商业 API** | 2 | 3 | 3 | 2 | **5** | **5** | 3 | **5** | **5** |
| **F · 开源现成方案** | 4 | 3 | 4 | 3 | 2 | 3 | 4 | **5** | 2 |
| **G · 混合方案**（B主+A降延迟+C兜底） | **5** | 4 | **5** | **5** | 3 | **1** | **5** | 2 | 3 |
| **H · 不采集只建模** | **1** | **1** | 2 | **1** | **5** | **5** | 2 | **5** | **5** |

**本原型的选型结论**：

| 选择 | 方案 | 理由 |
| --- | --- | --- |
| ✅ 已落地 | **C · 浏览器自动化** | 完整性 5 分（最高），且代码已存在、已验证可用 |
| 🎯 下一步 | **B · GraphQL 逆向** | 工程性价比最高（综合 3.55），无浏览器开销 |
| 📌 保留 | **A · 长连直读** | 延迟上限（0.5–3s），但**依赖风险仅 1 分**，不可单用 |
| 📥 仅供回填 | **D · 聚合站** | **时间分辨率 1 分**，无法还原跳动过程，禁用实时 |
| 📏 仅作基准 | **E · 商业 API** | 稳定性双满分，但**不覆盖目标平台**，只用作 fair line |

> **矩阵传达的核心矛盾**（报告 §12.5）：满足完整性（G）必须接受维护成本 1 分；
> 成本可接受（D/E/H）则接入层级或时间分辨率必然跌至 1–2。**没有任何一格是双优的。**

---

## 12. 已知问题与技术债

> 完整台账见 [AGENTS.md §7](AGENTS.md)。以下为影响使用的高优先级项。

| # | 问题 | 影响 | 状态 |
| --- | --- | --- | --- |
| 1 | **`config.py` 含明文 API Token**，且已随 git 历史泄漏 | 安全 | ⚠️ **需人工轮换密钥**（不擅自改写） |
| 2 | `main.py:149`、`main_optimized.py:390` 含明文 `captcha_token` | 安全 | ⚠️ 同上 |
| 3 | `main_optimized.py` 与 `main.py` 平行副本 | 可维护性 | 📋 待合并 |
| 4 | `web/*/main.py` 与根目录文件重复 | 可维护性 | 📋 待确认引用后清理 |
| 5 | ~~0 个测试文件~~ | 质量 | ✅ 已建 `tests/`，现 **10 个文件 / 429 用例** |
| 6 | `chromedriver.exe`（20MB Windows 二进制）入库 | 仓库体积 | 📋 评估改由容器内获取 |
| 7 | 死分支 `origin/feature/add-sportsdata-searcher`、`origin/new` | 治理 | 📋 确认后清理 |
| 8 | 调试脚本遗留根目录（`测试TypeError修复.py`、`搜索.py`） | 整洁度 | 📋 归入 `tests/` 或 `tools/` |

### 12.1 `browser_scraper_service.py` 的反直觉逻辑

```python
# browser_scraper_service.py:119
if os.getenv('DISPLAY'):
    options.add_argument('--headless')
```

**语义是「当 `DISPLAY` 存在时启用无头模式」** —— 名称与效果相反。
`docker-compose.yml` 中设置 `DISPLAY=:99` 实际是**触发无头模式的开关**，而非连接 X 服务器。
目前行为可用（已验证 healthy），但**不建议在未理解该逻辑前修改 `DISPLAY`**，否则会切到有头模式且无 X 服务器可用。

### 12.2 已修复（本轮）

| 问题 | 修复 |
| --- | --- |
| `docker compose` 引用根目录 `Dockerfile` 但**文件不存在**，构建直接失败 | 新增 [Dockerfile](Dockerfile)，已验证构建成功 |
| `Dockerfile.browser` 的 `COPY requirements-browser.txt` 与 context(`..`) 不匹配 | 改为 `COPY docker/requirements-browser.txt` |
| `Dockerfile.browser` 的 `COPY . .` 会把 `output/` 与 `config.py` 带入镜像 | 改为白名单仅复制 `browser_scraper_service.py` |
| `version: '3.8'` 触发 Compose V2 弃用警告 | 移除，改用 `name: ai_football` |
| 端口固定导致与宿主机其他项目冲突 | 改为 `${REDIS_PORT:-6379}` 等可覆盖形式 |
| 仓库无 `.gitignore`，680 个运行产物被追踪 | 新增 `.gitignore` + `git rm --cached`（本地文件保留） |

### 12.3 已修复（多玩法轮次）

| 问题 | 影响 | 修复 |
| --- | --- | --- |
| **采集器只认 HAD/HHAD，静默丢弃 TTG/CRS/HAFU 赔率** | 玩法覆盖严重不足 | 新增 `parse_pooled_odds`，覆盖全部 5 种官方玩法 |
| **双重机会被当作完备划分归一化，概率静默砍半** | 概率错误且不报错 | 引入 `ResultSpace.OVERLAPPING`，和为 2 而非 1 |
| **`float(10**400)` 抛 `OverflowError` 穿透校验层** | 异常未受控 | 转换助手统一捕获 `OverflowError` |
| **edge 公式误写为 `(p_model − p_market) × odds`** | 恒为负，掩盖真实正期望 | 修正为 `p_model × odds − 1` |
| **遗留市场名（1X2/AH）导致模型找不到市场对照** | 对照静默缺失 | 加入市场名规范化映射 |
| **API 路由器只认死字面量 `<id>`** | `/markets/<id>/<market>` 永不匹配 | 改为通用占位符 `<name>` |
| `goalLine` 缺失时每场刷 4 条噪音告警 | 告警淹没真实问题 | 改为可选字段，仅非法值才告警 |
| `ValuationService` 硬编码 `prefer_redis=False` | 容器内永不用 Redis，缓存不跨进程 | 按 `REDIS_URL` 自动判定 |
| 启动脚本重复运行导致端口漂移 | 端口不稳定 | 区分「我方占用」与「他人占用」，幂等复用 |

> ⚠️ **诚实声明**：以上均为开发过程中**实测发现**的真实缺陷，
> 每项都有对应的回归测试守护（见 `tests/test_markets.py`、
> `tests/test_multi_market.py`）。

---

## 13. 免责声明

本仓库是**足球数据的采集与量化研究工具**，用途为数据分析、算法研究与工程实践。

- **本项目不提供、不计划提供任何投注执行功能。** 报告中已论证该方向在经济与结构上不可行（§9.3、§13.2）。
- 报告中涉及的平台分析仅用于说明技术形态与风险，**不构成任何推荐或背书**。
- 采集功能应仅用于**公开可访问的公开数据**，且须遵守目标站点的服务条款、`robots.txt` 与适用法律。
  使用者对自身使用行为负全部责任。
- `config.py` 中的凭据**必须替换为你自己的密钥**，并建议改造为环境变量注入（AGENTS.md §3.4）。
- 报告中的经济学结论基于公开资料与透明数学推导，**未接触任何平台内部数据**；
  评分类结论为基于证据的主观判断，非测量值。

---

**📊 [研究报告](reports/leyu-kaiyun-odds-bot-feasibility/REPORT.md)** ·
**📐 [工程宪章](AGENTS.md)** ·
**🔍 [增强分析器说明](README_enhanced.md)** ·
**📁 [项目结构](项目结构.md)**
