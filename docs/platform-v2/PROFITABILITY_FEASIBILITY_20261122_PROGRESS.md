# 盈利可行性调研进度（2026-11-22）

## 2026-11-22 07:35 UTC · 项目与证据梳理开始

- 当前目标：基于实盘账本、可执行价格和独立研究，归因当前损失，判断专家跟单、市场辅助和预测算法的盈利可行性，并定义可证伪的无资金验证。
- 已完成证据：`git status --short --branch`（退出 0）确认当前任务分支及已有未提交工作；`git log -8 --oneline`（退出 0）确认 `1f943aa7` 为最近的自动订单回撤审计，已有冻结纸面策略；`sed -n '1,170p' README.md`（退出 0）确认业务链路；`rg --files docs reports infra tests` 与相关路径过滤（退出 0）定位审计、预注册和验证工具；`docker ps --format ...`（退出 0）确认项目审计容器可供后续只读核验。
- 活动工作包：项目业务与账本证据梳理，责任范围 `services/`、`infra/profitability/`、`reports/`，状态研究中。已有证据为回撤审计、结算工具和纸面策略登记。按实盘、历史仿真、前瞻纸面分开列样本，确认时间窗、口径与缺失项。
- 依赖及下一步：阅读最新回撤与纸面登记后委派 Pi 独立调研；核实只读权限后查询最小必要聚合。查询失败时记录原因并使用现存带时间戳的证据。

## 2026-11-22 07:36 UTC · 两路研究开始

- 已完成证据：`git show --format=short 1f943aa7`、`sed` 读取固定登记（均退出 0）。最近已核验的 32 笔净额 −36.83，全部成交价等于同次预检，策略状态和赔率盈利证据需要分别确认。登记的纸面窗为 11 月 20 日至 12 月 18 日 05:00 UTC。首次路径搜索因无效的附加路径退出 2，从明确路径独立重读成功。
- 活动工作包：Pi 经 T3 原生委派进入只读独立研究，启动返回 `starting`；主线程负责生产账本的限量只读聚合、前瞻样本、真实代码边界以及公开原始研究核查。责任范围 `reports/`、`infra/profitability/`、`services/`。按同一统计截止与回执口径复算，避免滚动窗口与固定入场队列混比。
- 依赖及下一步：核对当前部署及损益条件；分别识别可测的执行差和未证实的赛果优势；收集可复现实现的许可证、证据和适用边界。遇验证失败保留实际退出码和缺失字段。

## 2026-11-22 07:40 UTC · 实盘增量证据

- `docker exec -i ai-football-v2-api-1 python - < .runtime/profitability-feasibility-20261122/live_snapshot.py` 退出 0。固定入场队列截至 07:40:22 UTC 为 116 笔自然自动受理、106 笔已结、85 场、已结本金 530、回执净额 −79.33、回执 ROI −14.97%，10 笔待结。106 笔均未通过观察模式独立定价，101 笔成交价等于预检。106 笔均无已核验费用和币种。没有输出账户、订单标识或凭证。
- Pi 默认目标首次及配置刷新后同目标重试均告失败，模型认证返回 `401 invalid_api_key`。T3 目录显示另有已配置模型，已向用户询问选用方式。Codex 的独立公开研究与无资金试验证据核验继续。
- 下一步：核验分钟采样连续性、合格合同和成本证据，落实到固定的观测窗口。独立计算赛事相关性、回撤与识别限制，筛查可移植的开源研究。

## 2026-11-22 07:45 UTC · 纸面覆盖与时点核验

- 用户指定通过 Pi 使用 DeepSeek V4.1 Flash。T3 目录确认该模型后开启新的只读子任务，启动返回 `starting`。
- 无网络 Docker 只读挂载中运行 `.runtime/profitability-feasibility-20261122/paper_coverage.py`，退出 0。冻结检查 3,040 分钟，582 条不同建议、4 条原生预检、0 条外部合格参考，14 个清单赛事中 0 个与纸面预检赛事相交，独立合约批准与可验成本文件均为 0。
- 使用相同入场起点重跑 `live_snapshot.py`，退出 0，输出到私有聚合归档。07:44:14 UTC 同一批 116 笔受理中已结 107 笔、86 场、净 −75.43、ROI −14.10%，9 笔待结。116 笔原生入场距信号生成均少于 30 秒，分钟研究采样至少 30 秒，解释纸面报价覆盖与实盘机会时点之间的结构性差异。最新已结 107 笔均无费用与币种字段，回执金额检查无缺失或差异。
- 活动工作包：整理亏损桥接、公开实现的许可证和证据等级、现金流与会员数据产品的验证预算。下一步输出研究报告，核对 Pi 报告与已知证据，并保留已登记路径与新增捕获方案的协议边界。

## 2026-11-22 07:52 UTC · Codex 研究与取证校验通过

- 报告已覆盖历史和当前实际回执、时点偏差、市场基准、统计模型、套利和项目经营利润。[盈利可行性报告](../../reports/profitability-feasibility-20261122.md)
- 纸面复跑退出 0，私有聚合另存 `.runtime/profitability-feasibility-20261122/paper-aggregate.json`。实际命令如下，管道保留退出码。

  ```sh
  set -o pipefail
  docker run --rm --network none --cpus .5 --memory 256m --user "$(id -u):$(id -g)" \
    --mount type=bind,src=/root/ai_football/.runtime/profitability-simulation-20261120,dst=/trial,readonly \
    --mount type=bind,src=/root/ai_football/.runtime/automatic-price-observation-20261120/reference,dst=/reference,readonly \
    -i ai-football-verifier:v2.0.0-dev python - \
    < .runtime/profitability-feasibility-20261122/paper_coverage.py \
    | tee .runtime/profitability-feasibility-20261122/paper-aggregate.json
  ```

- 使用同一 Docker 镜像、`--network none --cpus .5 --memory 128m`，两个只读目录对聚合本金、净额、收益率、比分/时间覆盖、登记窗和报告数值执行 12 项断言，退出 0。`git check-ignore` 退出 0，运行文件留在私有忽略目录。T3 图表预览两次因服务端 Chrome 启动退出 1；内嵌渲染已返回附件，预览高度使用估算。
- 活动工作包：验收公开资料和 Codex 报告，并合入 Pi 的独立证据。当前已有的论文与源码仅作为诊断和离线基线。四周窗口仍开放，场馆费用、参考合同及新的样本覆盖留待后续验证。部署与自动投注授权通过只读方式核对。
