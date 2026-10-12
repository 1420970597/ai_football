# 决策正确率最大化研究

本目录记录基于当前 ai_football 代码、历史审计结果和互联网公开资料的决策模型优化研究。目标是提升同一决策截点上的概率质量与结算正确率，同时保留覆盖率、盘口类型、赔率和实际执行成本，避免用提高筛选阈值制造虚假的“命中率提升”。

## 文件

- `REPORT.md`：结论、证据、方案比较、实施计划和验收标准。
- `REPORT.html`：由仓库内 Markdown 转换脚本生成的单文件阅读版本。
- `data/current_snapshot.json`：生成时的代码、模型状态、数据规模和历史审计快照。
- `data/research_sources.csv`：互联网来源、证据等级、可核验结论和适用边界。
- `data/priority_matrix.csv`：P0-P4 优化项及验收门槛。
- `data/confidence_accuracy.csv`：按决策时点重放的综合置信度分箱统计。
- `scripts/build_assets.py`：只使用 Python 标准库生成上述数据资产。

## 复现

```bash
python3 -m py_compile reports/decision-accuracy-maximization/scripts/build_assets.py
python3 reports/decision-accuracy-maximization/scripts/build_assets.py
python3 reports/decision-model-accuracy/scripts/md_to_html.py \
  reports/decision-accuracy-maximization/REPORT.md \
  reports/decision-accuracy-maximization/REPORT.html
```

本报告不启动训练、不启用真实投注，也不把影子预测当成生产效果。外部论文中的样本、联赛、交易所和收益结果只用于选择研究方向，不能直接迁移为本项目的效果承诺。
