# 决策算法与模型文献审计

文献/PG测量日期2026-10-13，最终整合2026-10-14，基于最新主分支`f2d1aa6`，保留#60报价冷却与#61七坐标堆叠学习器。

严格纳入**算法66篇独立论文、直接足球预测模型51篇独立论文**。算法124条审计记录中58条未纳入；模型109条中29条辅助、29条排除。每行保存具体方法、代码映射、来源核验、阅读深度与证据边界。这是摘要/元数据级审计，未阅读全文或复现论文实验。

- [报告](REPORT.md)与[HTML](REPORT.html)：文献对应、公式、修复范围、其他agent issue复核与优化计划。
- [算法CSV](data/algorithm_paper_audit.csv)、[模型CSV](data/model_paper_audit.csv)：逐篇结果。
- [算法审计说明](data/algorithm_review_notes.md)、[模型审计说明](data/model_review_notes.md)：筛选/排除和阅读限制。
- [统计manifest](data/audit_manifest.json)、[图表CSV](data/audit_summary.csv)、[数量图](images/audit_counts.png)：纳入数量，不是准确率。
- [PG数据测量](data/pg_dataset_verification.json)、[PG采样检查](data/pg_sampling_verification.json)：真实只读数据与性能限制。
- [验证结果](data/validation_results.json)：实际命令、退出码与测试源文件哈希。

市场先验+残差方向合理，但验证与执行有重大偏差。本PR修复采样、校准可见性、保存权重对应和实际执行子群门控，并继续实现完整context、新代验证契约、原子现金预算、独立shadow收益和训练完成cohort；较早新代资格探针为 3,154 条输入、5 场比赛、qualified cohort 0，因此训练保持等待，不能复用旧模型。已结回执首100条缺陷已实现后台逐轮补扫，最终发布源核验、3个新刷新周期及浏览器检查通过；关闭投注时未调用真实场馆补扫。工程证据及尚待前瞻统计的项目见[#55](https://github.com/1420970597/ai_football/issues/55)。[#58](https://github.com/1420970597/ai_football/issues/58)的新旧价证据改变优化顺序：先保证输入和评价有效，再做模型增量。

复现文献数量与HTML：

```bash
python3 reports/decision-algorithm-model-audit/scripts/verify_literature.py
python3 reports/decision-model-accuracy/scripts/md_to_html.py \
  reports/decision-algorithm-model-audit/REPORT.md \
  reports/decision-algorithm-model-audit/REPORT.html
```

最终freeze-v6：1,716项测试、跳过14、99.110秒、退出码0；mypy120文件、ruff、184个Python编译与15个JS检查通过。真实PG核心/主要实验三轮ready，诊断partial显式为下界；较早history发布263.66秒18次HTTP200及至少3刷新周期、真实浏览器结构化检查通过。设置version52和SHA256保持，投注关闭、训练开启。资金补扫隔离PG24项断言通过，最终发布186.98秒13次HTTP200、4容器healthy、3个新的核心/实验刷新及生产浏览器通过。关闭投注时原生补扫not_started，现场分页与长期收益未由该观察验证。

原检索CSV与GitHub目录不参与严格论文计数，也不证明成熟开源方法或盈利。本轮不强制晋升候选、不提交真实投注；完整训练时长、状态可比CLV、真实成本后收益及独立增量仍需前瞻证据，见[#55](https://github.com/1420970597/ai_football/issues/55)与[#58](https://github.com/1420970597/ai_football/issues/58)。
