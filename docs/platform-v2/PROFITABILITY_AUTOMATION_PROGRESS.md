# 盈利分析自动汇总

## 2026-11-19 13:16 UTC · 研究中

- 目标为定时生成可复算的生产分析，覆盖专家与审核、乐鱼盘口与成交、风险回放、回执与资金以及可用的场外价格证据。责任目录为 `infra/profitability`、`tests` 和本进度记录。报告保存在项目私有运行目录。
- `git status --short --branch`、`docker ps`、`systemctl list-timers` 和现有入口阅读均退出 0。生产已有五分钟快照、分窗对账、钱包快照及独立场外试点。当前缺少统一的定时输出和单项失败提示。
- 依赖生产 API 的现有只读分析连接、五分钟快照、分窗对账归档及场外回放归档。准备复用已验证的诊断 SQL，统一分析截止时刻，保留各查询原有的事件时间口径。
- 验收包括 Docker 隔离验证、真实生产只读运行、私有报告原子发布、定时器实际执行和错项显式反馈。资金权限、生产交易策略、源频道和产品逻辑不参与变更。外部合约、费用及正式钱包账单如缺失，在报告中明示。

## 2026-11-19 13:21 UTC · 代码已写，首份生产只读报告已生成

- `tar … | docker exec … python …` 的第一次完整查询退出 0，12 项 SQL 全返回聚合结果。`bash infra/profitability/collect_analysis.sh .runtime/profitability-watch` 退出 0，已发布首个私有目录和 `latest` 链接。报告按原始时钟分别显示推荐、交易受理和结算。
- 来源回放未触发候选剔除，正式场外回放和烟测分开标记。后一阶段补充报告端的缺口与风险摘要，并覆盖观测文件轮换、任务失败、快照新旧和报告权限。尚未登记新定时器。

## 2026-11-19 13:25 UTC · 隔离验证通过

- Docker `python -m unittest discover -s tests -p test_profitability_analysis.py` 四项退出 0。Docker `bash -n`、`bash tests/integration/profitability_reconcile_due_windows.sh` 和宿主 `git diff --check` 均退出 0。覆盖参数类型兼容、过期结算拒绝、场外归档时序、局部错误、定时器重建参数和活跃任务复用。
- 当前活动阶段为生产定时发布验收，核对现有任务与新增定时器的运行结果、实际报告时点、十二项查询状态及文件权限。报告采用每小时第 55 分钟的 UTC 周期，遵循现有瞬态定时器的重启恢复入口。

## 2026-11-19 13:28 UTC · 生产定时发布验收通过

- `bash infra/profitability/restore_reconciliation_timer.sh` 退出 0，新增定时器为 active，首次自动运行 13:25:45 UTC，约三秒后发布。更新回放时间口径后，`systemctl start ai-football-profitability-analysis.service` 退出 0，13:27:23 UTC 发布下一份报告，十二项诊断查询均成功，Markdown 与 JSON 权限均为 0600。定时周期仍为每小时 UTC 第 55 分钟。
- 首次报告中一笔刚受理记录缺可用受理证据，导致两个回放无比较行。回放增加五分钟受理截点缓冲，保留查询中的证据校验，报告据实展示无效记录和无结果。两项回放已恢复输出。Docker `python -m unittest tests.test_profitability_analysis tests.test_profitability_reconcile_upstream` 共 24 项通过，`git diff --check` 退出 0。
- 资金证据仍依赖乐鱼完整币种、费用及正式钱包账单。场外合约依赖后续正式自然预检回放，已定时采集的场外试点按其原日程继续。报告每小时保存在项目私有目录，复算数据及源归档不纳入 Git。主机重启后通过现有恢复入口重建瞬态定时器。
