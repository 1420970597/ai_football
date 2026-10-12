# 自动下注亏损审计与修复（TASK-40 / TASK-41）

分析部署版本 `4f3861b` 的决策、结算、训练与执行链，并用场馆真实结算逐笔核验。

- [完整报告](REPORT.md)
- [浏览器版](REPORT.html)
- [冻结统计](data/summary.json)
- [160笔自动订单与场馆结算对照](data/receipt_comparison.csv)
- [代码/数据截点与哈希](data/capture.json)

关键发现：滚球让球结算口径错误、负号拆盘错误、单元素概率归一化错误；这些问题污染收益历史和算法权重。真实自动订单样本净亏10.67元，现有代码按同一成交条件重算却显示净赚235.83元。

`scripts/audit.py` 读取冻结CSV重算归因；`scripts/reproduce.py` 用5项离线诊断验证缺陷和参数边界；`scripts/make_figures.py` 在带中文字体的容器中重建全部派生CSV和3张图；`scripts/md_to_html.py` 复用既有Mermaid渲染器。

金额是已结算样本的测量值，订单号已替换为不可逆摘要。报告包含5笔未能与本地自动订单匹配的账户记录，但它们单独统计，未归因给自动策略。没有执行新投注、改动实盘配置或修改业务模块。

TASK-41 已修复业务链路并评估训练污染，见报告第12节、[训练污染测量](data/training_contamination.json)与[修复核验](data/repair_verification.json)。旧版概率特征隔离，旧模型退出活动位置；自动下注保持关闭，新版收集干净样本。`training_audit.py --backup <一致备份目录>` 重建旧验证成绩，再按修正目标评分。前述“没有改动实盘”的表述仅指 TASK-40 原始审计阶段。
