# 独立策略审计

报告正文：[REPORT.md](REPORT.md)。

- `scripts/roi_evidence.py`：从生产 PostgreSQL 复现各口径 ROI 与按场 bootstrap 区间，输出 `data/roi_evidence.json`。
- `scripts/post_goal_picks.py`：从一小时回放日志统计推荐距最近比分变化的秒数。
- `data/papers_*.csv`、`data/oss_*.csv`：论文 137 篇、开源项目 140 个的核实清单。

运行前需加载仓库根 `.env` 中的 `PG_*` 变量；脚本只读，不写生产库。
