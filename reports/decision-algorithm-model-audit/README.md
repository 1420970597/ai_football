# 决策算法与模型文献审计

文献/PG测量日期2026-10-13，最终整合2026-10-14，基于最新主分支`f2d1aa6`，保留#60报价冷却与#61七坐标堆叠学习器。

严格纳入**算法66篇独立论文、直接足球预测模型51篇独立论文**。算法124条审计记录中58条未纳入；模型109条中29条辅助、29条排除。每行保存具体方法、代码映射、来源核验、阅读深度与证据边界。这是摘要/元数据级审计，未阅读全文或复现论文实验。

- [报告](REPORT.md)与[HTML](REPORT.html)：文献对应、公式、修复范围、其他agent issue复核与优化计划。
- [算法CSV](data/algorithm_paper_audit.csv)、[模型CSV](data/model_paper_audit.csv)：逐篇结果。
- [算法审计说明](data/algorithm_review_notes.md)、[模型审计说明](data/model_review_notes.md)：筛选/排除和阅读限制。
- [统计manifest](data/audit_manifest.json)、[图表CSV](data/audit_summary.csv)、[数量图](images/audit_counts.png)：纳入数量，不是准确率。
- [PG数据测量](data/pg_dataset_verification.json)、[PG采样检查](data/pg_sampling_verification.json)：真实只读数据与性能限制。
- [验证结果](data/validation_results.json)：实际命令、退出码与测试源文件哈希。

市场先验+残差方向合理，但验证与执行有重大偏差。本PR修复采样、校准可见性、保存权重对应和实际执行子群门控；完整context、旧artifact契约、资金预算、shadow走盘本金和训练调度仍在[#55](https://github.com/1420970597/ai_football/issues/55)。[#58](https://github.com/1420970597/ai_football/issues/58)的新旧价证据改变优化顺序：先保证输入和评价有效，再做模型增量。

复现文献数量与HTML：

```bash
python3 reports/decision-algorithm-model-audit/scripts/verify_literature.py
python3 reports/decision-model-accuracy/scripts/md_to_html.py \
  reports/decision-algorithm-model-audit/REPORT.md \
  reports/decision-algorithm-model-audit/REPORT.html
```

原检索CSV与GitHub目录不参与严格论文计数，也不证明成熟开源方法或盈利。本轮未部署、未晋升候选、未提交真实投注。
