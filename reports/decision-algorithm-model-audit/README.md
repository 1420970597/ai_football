# 决策算法与模型文献 / 开源审计

本目录记录对当前 ai_football 决策链路的文献与开源对照审计。研究范围分为两组，各配论文清单与开源项目清单：

- `algorithm_papers.csv`：预测评估、概率校准、赔率去水、融合、时间验证、下注与风险决策等算法方法（80 篇）。
- `model_papers.csv`：足球/足球滚球的 Poisson、Dixon-Coles、动态强度、机器学习、神经网络、进球危险率和 xG 等模型方法（80 篇）。
- `oss_algorithm_projects.csv`：投注决策/注码/去水/校准/回测/风控相关开源项目（100 个）。
- `oss_model_projects.csv`：比分预测/Poisson/Dixon-Coles/滚球/xG/评级等开源项目（100 个）。

文献元数据由 OpenAlex 公共 API 串行抓取并保存检索时间、查询词、DOI、作者、摘要片段和引用量；开源项目元数据由 GitHub 检索 API 获取并保存查询词、相关度评分、star 数、推送时间与 topics。元数据可复核，但外部论文与仓库结果不等同于本项目测量结果。

`REPORT.md` 给出系统设计合理性总评、真实亏损归因（交叉引用 `reports/auto-betting-loss-audit/`）、160 篇论文与 200 个开源项目的对照审计、P0–P3 修复计划和验收口径。报告发现的问题分为“统计/实现偏差”和“待验证优化”，不会把论文或仓库中的外部收益数字直接迁移到乐鱼数据。

远程 issue：[#55 决策链路文献与开源审计 —— 亏损归因、设计合理性与 P0–P3 优化建议](https://github.com/1420970597/ai_football/issues/55)。

复现：

```bash
python3 -m py_compile reports/decision-algorithm-model-audit/scripts/collect_literature.py \
                        reports/decision-algorithm-model-audit/scripts/collect_oss.py
python3 reports/decision-algorithm-model-audit/scripts/collect_literature.py
python3 reports/decision-algorithm-model-audit/scripts/collect_oss.py
./scripts/cpu-limited.sh run -- python3 -m unittest \
  tests.test_training_data tests.test_learned_model tests.test_model_training -q
python3 reports/decision-model-accuracy/scripts/md_to_html.py \
  reports/decision-algorithm-model-audit/REPORT.md \
  reports/decision-algorithm-model-audit/REPORT.html
```
