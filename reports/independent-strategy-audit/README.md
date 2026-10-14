# 独立策略审计：2026-10-13 历史归档

报告正文：[REPORT.md](REPORT.md)。

- `data/archive_manifest.json`：固定附件 SHA256、来源报告提交 `d05451e5d7dda7e21f05c322e7023b3416e5a324` 和更正范围；历史 `roi_evidence.json` 保持原字节。
- `scripts/roi_evidence.py`：只读查询当前 PostgreSQL 并结合指定本地订单 CSV 重算 ROI/按场 bootstrap；没有 as-of 或算法版本过滤，不能复现固定历史快照。默认输出项目 `output/independent-strategy-audit/current/`，不覆盖归档。
- `scripts/post_goal_picks.py`：统计指定单小时回放首次推荐距比分变化的秒数；输出比赛别名和相对时间，不输出绝对发布时间。
- `data/papers_*.csv`：137 个分类条目，按 `re.sub(r'[^a-z0-9]', '', title.casefold())` 得到 119 个规范化标题组；不是 DOI/独立研究身份核验或全文审读。
- `data/oss_*.csv`：140 个分类条目、128 个唯一仓库名；未逐项审核成熟度、源码或收益。

79.4445% 是历史非虚拟模拟子群净利润占比，不是旧价因果比例。−8.53% 是推荐赔率与本地结算重算，不是场馆权威盈亏。30/60 秒后价格漂移不直接等于状态可比 CLV 或实盘晋级证据。后续工程见 [PR #62](https://github.com/1420970597/ai_football/pull/62)，本文不描述当前生产状态。

两份逐条 CSV 使用一致比赛别名，去除绝对时间与联赛名，保留数值/行序/比赛分组；确定性公开别名不保证不可再识别。本次归档修正仅使用标准库本地检查，未查 PG 或重算 bootstrap。当前数据重算另需容器中的 `psycopg` 和 `.env` 提供的 `PG_*`，不在宿主机装包。
