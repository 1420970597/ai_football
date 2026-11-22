# 盈利实验计划与执行证据（2026-11-22）

## 08:34 UTC · 设计阶段启动

- 目标：由 Codex 与 Pi 独立提出各至少六项改进，逐一在仓库 Discussions 发布、审计及跟踪，再由 Pi 实现可复跑的隔离模拟，Codex 复核实现、回归和结果的证据等级。
- 当前证据：`git status --short --branch`、`git log -8 --oneline`、`gh repo view --json nameWithOwner,hasDiscussionsEnabled,visibility,viewerPermission,defaultBranchRef` 与 `gh api graphql` 查询类别均退出 0。现有任务分支为 `fix/TASK-78-hermes-telegram-sync`，起点 `f5942757`，有他人未提交文件；远端为公开仓库，Ideas 类别可用。讨论只含方案、去标识的计数及可公开方法。
- 当前阶段：`reports/` 和 `docs/platform-v2/` 研究中。以既有生产回执审计、冻结纸面登记及隔离容器为前置。已向 Pi（DeepSeek V4.1 Flash）提交独立设计任务，T3 返回 starting。Codex 独立撰写另一组方案，待两个设计集之后逐个写可执行审计。
- 磁盘与环境：`df -h /root/ai_football /var/lib/docker` 与 verifier 镜像检查退出 0，根卷可用 22 GiB。复用已有 verifier 镜像的固定摘要，测试容器使用独立命名、无网络、有限 CPU/内存及明确只读输入目录。磁盘不足先检查 Docker 对象引用与任务归属，再清理安全可回收构建缓存和已核实无容器引用的闲置镜像。
- 下一步：列 Codex 六项具体假设、范围及结果边界，准备可重复提交 Discussion、评论及回读验证的流程。远端失败时检查现有帖子再安全重试，避免重复讨论。

## 08:40 UTC · 方案已发布，逐项实现审计结束

- Pi（DeepSeek V4.1 Flash）提交八项独立方案，Codex 提交六项。T3 子任务状态为 completed。远程 `gh api graphql --input .../discussion-create.json` 退出 0，建立 14 个 Ideas 讨论（#80–#93）。远程 `gh api graphql --input .../discussion-audit.json` 退出 0，给每个讨论添加 Codex 审计与实施 TODO。审计核对了冻结数据库导出：历史已受理 453 条、专家建议 1,784 条，只有 248 条含各选项价，部分实验所需同期、同合同盘口与正式费用并不齐备。
- [独立方案及逐帖链接](../../reports/profitability-experiments-20261122.md) · [审计实施细则](PROFITABILITY_EXPERIMENTS_20261122_TODO.md)
- 下一步：以本次方案提交为底座，委派 Pi 在独立工作树实现。同一算法和数据处理复用到各实验，避免平行副本。Codex 准备本地仿真输入并核对实现；真实历史只按现有证据给结论。

## 08:43 UTC · 独立开发与容器验收准备

- `git commit --only ...`、`git push origin HEAD:refs/heads/fix/TASK-78-hermes-telegram-sync`、`git ls-remote --heads origin ...` 均退出 0，远端核对为 `09a1cf83009ad749859e0d09a4ab4e4b3e2f407c`。远端 GraphQL 回读确认 #80–#93 均恰有一条审计 TODO。
- Pi 两个独立 worktree 分别实现 C01–C06、P01、P05、P06 和 P02–P04、P07、P08，使用同一 DeepSeek 模型及不重叠的代码目录。Codex 负责无网络容器运行入口和冻结输入。
- 用 `curl --fail --location --proto '=https' ...` 下载英超和德甲各四季公开 CSV，退出 0，共 2,744 个有表头分离的样本行，固定每文件 SHA-256。公开 CSV 不写入 Git。Docker 磁盘检查仍有 22 GiB。本轮复用镜像并记录容器 ID 以复查现有服务，暂未达到清理条件。
- 下一步：只读导出固定分钟片段和既有原生回执。测试按 Git 提交提取最小源目录和已冻结证据，Docker 内执行，以便验收不读工作区其他文件。

## 09:09 UTC · 首轮实现审计与恢复

- 两个 Pi 工作树首轮均提前结束，没有可验收提交。保留各自实际写出的 `clock/inputs/stats` 与 `closing_line/evidence/public_results` 草稿，基础提交为 `26512394`。Codex 审到分钟预检方向、固定窗口分母和缺映射时不能一直硬编码拒绝等问题，已写入三次细化委派。三批明确分工，均由指定 Pi 模型执行。
- 冻结样本通过复制和摘要清单整备，3,040 帧和八份 CSV。输入清单 SHA-256 为 `72d9f6f425bf29c42437f3483282c3a98ca9d1f88e90321c4a6d35c1991151e8`。
- `bash infra/profitability/experiments/run-isolated.sh 09a1cf83 ... -- -` 的边界探针退出 0，核验仅有回环、无生产配置与 Docker socket、输入无法写入。首次广义回归退出 1，发现容器临时目录不允许测试用本地命令和源归档漏带服务导入；修正隔离入口后重跑 70 项，退出 0，其中 2 项需要隔离 PostgreSQL。独立测试数据库首次执行退出 1（运行 UID 无法读取临时源目录），改为指定属主重跑 70 项全部通过，退出 0。数据库只附加新建的 Docker internal 网络及临时内存目录，运行后清理任务容器和网络。
- 下一步：等三批交付时继续检查公开或冻结输入及结果发布条件。产品代码、生产开关、已有业务容器均未操作。

## 09:14 UTC · Codex 建立独立回放对照

- 隔离入口使用固定摘要镜像复跑既有 `simulate_strategies`、`paper_validate` 与 `market_adjusted_experts`，组合进程退出 0。历史对照 451 笔受理、443 笔可结算复算、282 场，压力后一单位 ROI −11.20%。3,040 个纸面分钟档中 4 笔原生预检、4 笔缺外部合同。专家后段同价组 212 个可结、78 场，两档收缩组分别只有 24 和 5 个可结。
- 同一隔离命令核查公开 CSV 共 2,744 场、8 文件、零跳过。公共多公司均价在 2,744 场的归一化三项对数损失分别为较早快照 `0.968042`，闭盘 `0.964571`。闭盘快照只在事后对比。对照录入私有聚合，不用作实盘收益。 `sha256sum -c --quiet ...` 与生产容器 ID 对比均退出 0。
- 下一步：用上述独立输出核对 Pi 的研究结果和分母。出具实盘不可验证原因时保留已完成的全部隔离测试和实际有样本分支。

## 09:44 UTC · 全部实验实现、隔离验收及逐帖回填完成

- Pi 完成实现并交付四组可整合代码，含真归档检查与边界测试。原始提交包括 `79349f32`（时钟、归因与价格桥）、`d708c75a`（经济与样本）、`0c5f087e`（容量与敞口）、`4bf26248`（市场与时机），以及 `cc7bc2e0`（比分模型与边界）。Codex 按审计要求修正了未来赛季泄漏、错误的预测校准表、报价路径的认证成本校验、无证据的跨场所合同确认、迟到回执及重采样样本口径。改动全部在研究目录及测试文件。
- 集成第一次执行 `unittest discover -s tests -p 'test_profitability_experiments*.py'` 退出 1，因构造样例缺日期字段。加上日期并补回执版本测试后，固定源码 `85dc5279` 的 74 个新增测试全部通过，退出 0。用内部网络专用 PostgreSQL，加挂同一组只读回放和公开数据，`unittest discover -s tests -p 'test_profitability*.py'` 运行 144 项全部通过，退出 0。
- `bash infra/profitability/experiments/run-isolated.sh 85dc5279 <冻结输入目录> <公开数据目录> -- -m infra.profitability.experiments` 两次退出 0，`cmp` 退出 0。结果 SHA-256 为 `eaf307d262d8a64193bc179e78f2e94b53f46fc440903b6627abbcf3b3ebf578`，输入总摘要 `e7b70f9f31ed3cb69138349ef6b3b8ca4dd98602010ab2d0714c88e013d5cb1a`。
- `gh api graphql --input .../discussion-results.json` 和 `gh api graphql --input .../discussion-readback.json` 均退出 0，确认 14 个实际结果均回复于对应的 Codex TODO。 [逐项结论及回帖](../../reports/profitability-experiments-results-20261122.md)
- 生产容器 ID 对比、冻结输入校验与 Git 文本检查退出 0。阶段只进行冻结历史和纸面验证，费用、独立合同、成交深度与新时段仍是收益验收依赖。原四周登记和生产配置未触及。磁盘 22 GiB，未达清理条件。T3 图表预览 Chromium 启动退出 1，使用估算高度送内嵌图表，未获得服务端截图验证。
- 下一步：同步已验收代码和结果到当前任务分支，回读远端提交。后续投入优先用于实验 C01/C02/P01/P07 所需的同期报价、合同映射及账户成本。

## 09:46 UTC · 任务分支远端验收

- `git diff --check 09a1cf83..HEAD`、敏感模式扫描、`git push origin HEAD:refs/heads/fix/TASK-78-hermes-telegram-sync` 和 `git ls-remote --exit-code --heads origin refs/heads/fix/TASK-78-hermes-telegram-sync` 均退出 0。远端逐位核对已验收代码与结果提交 `1257a7ab406ff6fd1a36d0f4e857ddcc1951d99a`。两次回放与 144 项通过结果关联固定源码 `85dc5279`，随后仅移除空行并增加文档。远端当前保留研究变更，无打开的同分支 PR。
- 本轮设计、发布、审计、Pi 实践、Codex 隔离验收与回帖均完成。实际收益与费用证据仍待后续研究，不进入资金执行。
