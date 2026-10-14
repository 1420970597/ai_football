# 决策算法论文逐篇审计说明

审计日期：2026-10-13。该文件与 `algorithm_paper_audit.csv` 配套，审计对象是原始 `algorithm_papers.csv` 的 80 条记录及补充的可核验方法论文。该审计只研究决策算法、概率评估、市场去水、融合、时序验证和资金风险；不把论文的预测准确率或历史收益直接外推为当前乐鱼滚球收益。

## 计数和证据边界

| 口径 | 数量 | 说明 |
|---|---:|---|
| 原始检索记录 | 80 | OpenAlex 目录记录，不等于 80 篇独立论文 |
| 补充记录 | 44 | 通过 DOI 注册信息或官方摘要页逐条核验 |
| 审计总记录 | 124 | CSV 每行一个来源记录 |
| 纳入 | 66 | 具有可迁移方法、且来源可核验 |
| 纳入的独立 DOI | 65 | 同一 DOI 只计 1 篇 |
| 纳入的无 DOI来源 | 1 | 英超研究的 RePEc/OpenAlex 记录，来源可核验但未分配 DOI |
| 排除 | 58 | 重复版本、非算法主题、来源或方法边界不足等 |

因此算法侧独立相关论文为 **66 篇（65 个独立 DOI + 1 个可核验无 DOI 记录）**，达到“至少 50 篇”的要求。纳入项中 4 篇是足球/体育直接证据，62 篇是可迁移的统计或经济决策方法。可迁移证据不能被表述为足球投注盈利证据。

证据深度在 CSV 的 `review_depth` 字段中逐篇标记：

- `元数据+摘要`：补充论文的 Crossref 或 arXiv 官方记录返回摘要，并核验题名、年份、DOI。
- `目录元数据+摘要片段`：原始 OpenAlex 记录含摘要片段，结合题名、作者、期刊和 DOI 复审。
- `元数据核验；方法摘要来自既有学术知识，未阅读全文`：DOI/题名已核验，但出版平台本轮未返回摘要；不把全文结果当作已阅读证据。

## 逐篇判定规则

CSV 的 `included=yes` 必须同时满足：

1. 论文讨论概率预测、proper scoring、校准、预测融合、赔率去水、时序/漂移验证、投注风险或足球赔率决策中的至少一项；
2. 题名和摘要/元数据能够说明方法，而不是仅出现 `forecast`、`bet` 或 `decision` 关键词；
3. DOI 或可核验的 OpenAlex/RePEc 记录可回溯；
4. 能映射到当前代码中的明确模块，或者能作为待实现挑战方法并明确标注。

以下记录不会增加独立计数：同一研究的 arXiv、会议摘要和期刊版本；作者回复或评论；只有信任、社会影响、医疗/天气应用结果而没有可迁移方法的文章；来源不完整且无法确认方法的条目；明显与概率决策无关的主题。排除理由保存在 `exclusion_reason`，不会静默删除。

## 当前代码映射覆盖

| 研究主题 | 当前实现位置 | 审计结论 |
|---|---|---|
| proper score、Brier、log loss、ECE | `core/calibration.py`、`core/model_validation.py:evaluate`、`core/learned_model.py:metrics` | 方向一致；需同时报告概率评分、HAD Top-1 和收益，不能以命中率代替校准 |
| 多盘口概率一致性 | `core/model_validation.py:coherent_probabilities`、`core/ensemble.py:cohere_candidates` | 方向一致；独立二元指标与完整盘口指标必须分开 |
| 赔率隐含概率和去水 | `core/devig.py`（proportional/additive/power/odds-ratio/Shin） | 方法覆盖较广；去水方法分歧应进入敏感性分析，不能预设 Shin 最优 |
| 算法/模型融合 | `core/ensemble.py:adaptive_weights`、`core/ensemble.py:pool` | 有按比赛聚类的收缩权重；仍需独立候选登记和多重搜索控制 |
| 概率后处理 | `core/probability_calibration.py:fit_logistic`、`core/oof_peer.py` | 已有 logistic/OOF 校准；Beta/Dirichlet/温度缩放只能作为样本外挑战者 |
| 时间隔离和滚动评估 | `core/model_validation.py:temporal_split`、`rolling_folds`、`core/learned_model.py:train` | 方向一致；任何调参都不能重用最终测试标签 |
| 配对市场基准 | `core/model_validation.py:paired_evidence`、`promotion` | 已有按比赛聚类 bootstrap；若搜索大量候选，应追加 Reality Check/SPA 或候选登记 |
| 概念漂移 | `core/model_validation.py:rolling_folds`、`core/ensemble.py:adaptive_weights` | 有滚动评估但不等于漂移检测；应监控分联赛、时段和盘口类型的校准退化 |
| Kelly/风险 | `core/economics.py:kelly_fraction`、`fractional_kelly`、`sizing`、`ruin_probability_bound` | 有分数 Kelly 和风险函数；全局未结算敞口/CVaR 仍是资金层缺口，不能宣称论文级风险约束已完整实现 |
| 赔率市场信息 | `core/devig.py`、`core/economics.py:expected_value`、`effective_ev` | 单一乐鱼来源是信息上限；论文中的多庄家/交易所结论不能直接外推 |

## 关键审计发现

1. **没有发现“模型架构完全偏离文献”的证据。** 当前以市场概率为 offset、学习残差、按完整盘口归一化、时间隔离、按比赛聚类评估，与主流概率决策研究一致。
2. **主报告的后续代码复审确认了重大验证偏差，需要在远程 issue 跟踪。** 包括最终校准使用尚未可见的标签、晋级评估与保存模型参数不一致、离线/实际路由的盘口后处理不一致，以及总体算法指标掩盖实际执行子群退化。全局资金敞口也尚未与执行层闭合。此前仅按文献方向判断“只有优化缺口”的结论已被代码反例推翻；本说明以主报告的最终修复状态为准。
3. **足球盈利证据很弱且不可替代。** 直接足球条目只说明概率建模或市场评估，不证明滚球自动投注稳定盈利。天气、医疗和电力论文仅支持统计方法迁移，不能拿来证明当前系统胜率。
4. **算法正确率和资金决策必须分层。** Brier、log loss、ECE、HAD Top-1、CLV、ROI、回撤和拒单/成交率是不同量，任一单项高于 80% 都不能单独证明可下注。

## 建议的后续优化（不构成已执行代码变更）

- 为每个候选模型记录搜索族、首次看到时间、测试集版本和晋级理由，按候选族进行 Reality Check/SPA 或预注册比较。
- 在目标联赛/盘口上比较 proportional、power、odds-ratio、Shin 的样本外 Brier、log loss、ECE、CLV 和 ROI；把方法间分歧作为门控特征。
- 按比赛、联赛、盘口类型和决策时段输出可靠性图、校准区间和尾部校准；小样本不输出虚假的高精度。
- 将分数 Kelly、概率参数不确定性、未结算总敞口和回撤/CVaR约束接入现金执行层；展示层 Kelly 数值不能代替订单限额。
- 继续保持训练输入的时间截点：决策时点之后的盘口、结算标签和任何未来派生字段都不得进入该决策的训练特征。

## 复核限制

本审计是可复现的元数据/摘要级研究，不声称已阅读全文 124 条记录。Crossref/OpenAlex 的题名、年份、DOI 和摘要是可核验来源；摘要缺失的条目标记为“元数据核验”，其方法描述使用标题、出版信息及已知论文方法，不能替代全文复核。论文结果跨联赛、跨运动、跨任务，不能直接作为乐鱼滚球的收益承诺。

后续复核补充了 7 篇核心方法的 OpenAlex 原记录完整摘要（proper scores、预测组合、二元/多类校准、预测比较、Reality Check、SPA），以及 Risk-Constrained Kelly Gambling 的 arXiv 官方摘要与 Stanford 作者页面，均返回 HTTP 200。当前纳入项阅读深度为：28 篇元数据+摘要、22 篇目录摘要片段、16 篇元数据与已知方法核验；没有将任何一篇标为全文审阅。自动完整性统计见 `audit_manifest.json`。
