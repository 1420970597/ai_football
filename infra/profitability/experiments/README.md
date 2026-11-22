# 隔离盈利实验

实验以既有不可变信号、报价、结算和公开历史赛果为输入。结果归类为历史回放、纸面覆盖、报价诊断或数据不足。应用业务、采集进程、部署和下单权限均使用原有代码与配置。

## 输入契约

| 输入 | 取数范围 |
| --- | --- |
| `native-cohort.json` | `infra.profitability.export_simulation` 的冻结原生受理导出。入场和回执截至、缺失字段、费用未知均遵循导出语义。无成交列的导出无法完整重放三段价格。 |
| `market-adjusted-cohort.json` | `market_adjusted_experts.py export` 保存的推荐、同步市场选项和版本化结算。 |
| `paper-preflight/` | 自然纸面分钟档。复制登记的精确范围后再运行，忽略随后到达的记录。 |
| `reference/` 和 `cost-evidence/` | 同合同独立盘口、规则散列和可信账户成本快照。保留缺失状态，不能用零成本代替。 |
| `/public` | Football-Data 公开许可范围内的英超 `E0` 与德甲 `D1` CSV，赛季 2022/23 至 2025/26，使用 `E0_2223.csv` 等命名。原始数据留在本地。 |

公开的赛季 CSV 可从同一来源重新取用。每次下载保留 SHA-256；数据修订后应作为新的研究输入。公开收盘快照用于事后参考，输入的可见性不得提前到原推荐时点。 [公开数据目录](https://www.football-data.co.uk/downloadm.php)

## 启动隔离运行

传入已提交的代码版本、冻结私有输入目录与公开数据目录。一次容器只运行一个命令。入口只从提交打包明确的代码路径，无网络、只读根和输入、独立临时目录、有限内存及 CPU。

```sh
bash infra/profitability/experiments/run-isolated.sh <源码提交> <冻结输入目录> <公开数据目录> -- \
  -m unittest discover -s tests -p 'test_profitability*.py'

bash infra/profitability/experiments/run-isolated.sh <源码提交> <冻结输入目录> <公开数据目录> -- \
  -m infra.profitability.experiments
```

使用本机既有 verifier 镜像摘要。环境校验时确认接口只有回环设备，输入及源码只读，没有生产环境文件和 Docker socket。公开报告仅保留合格计数、方法和可披露的汇总。依据每次输出的缺失原因判断是否能继续讨论成本后表现。

统一入口一次输出 C01–C06、P01–P08 的 JSON。历史受理样本、事后市场预测评分、金额压力和前瞻纸面数据各用独立证据口径。两项旧的数据库回归在没有独立 PostgreSQL 时会跳过，可另外按项目隔离测试流程执行。
