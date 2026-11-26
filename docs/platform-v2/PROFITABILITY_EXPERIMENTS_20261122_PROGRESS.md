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

## 10:03 UTC · 继续同期报价与费用诊断

- 目标：针对 C01/C02/P01/P07，在独立容器验证自然建议、原生预检与参考快照能否同期对齐，量化资料缺口并复用原冻结评估器校验费用；继续以可核验费后收益为终点。
- 当前证据：`git status --short --branch`、`git log -8 --oneline`、`df -h /root/ai_football`、`docker ps --filter name=ai-football-v2` 与读取登记均退出 0。起点 `659c0b84`，剩余约 22 GiB，原主策略四周窗至 12 月 18 日 05:00 UTC；已有未提交研究文件保留。
- 活动工作包：同期事件证据和参考合同覆盖（`infra/profitability/`、`reports/`，研究中），依赖已归档事件、只读行情及费用凭证。下一步检查原生报价的不可变来源与官方费用是否已可取得；费用或独立价格缺失时保留未验证原因。

## 10:18 UTC · 同期事件隔离验证通过，登记期自动跟进已启用

- `docker exec -i ai-football-v2-api-1 python - 2026-11-20T04:58:30Z 2026-11-22T07:40:00Z 2026-11-22T07:40:00Z` 通过只读可重复读事务导出不可变建议与预检事件，退出 0。原件 SHA-256 为 `6cccf7ae9030422335375f8795a342ec18c657e13f52a886c05eedaeb4b94f04`，仅私有留存。Codex 实现 `d0f4370b`，Pi（DeepSeek V4.1 Flash）独立核对持久化 ID、时点及费用口径。Codex 复核采用同建议 ID 和创建时刻对齐时间线，保留不同报价版本，不将同 ID 视为同一时点的可成交报价。修复提交 `a6327d45` 要求预检证据先于分钟观测落库。
- `bash infra/profitability/experiments/run-isolated.sh a6327d45 <冻结输入目录> <公开数据目录> -- -m infra.profitability.experiments.event_coverage ...` 两次退出 0，`cmp` 退出 0。结果 SHA-256 为 `920726be7d892c99525ef7877029d6df93ab2907fd0534376e1760a9794cda74`。3,040 帧中 582 条全部配对，137 条较早有完整原生预检，参考清单 14 场、批准合同 0、费用凭证 0。130 多条较晚受阻的记录只能用于定位时钟覆盖，尚无从核验恢复成交或费后优势。
- 固定源码的 `unittest discover -s tests -p 'test_profitability*.py'` 无网络运行 150 项、2 项缺数据库而跳过，退出 0。临时内网 PostgreSQL 首轮因别名未满足既有测试隔离断言，退出 1；修正专用内网别名后 150 项全部通过，退出 0。销毁本轮创建的临时测试库及网络。
- 从现有在制脚本快照整备冻结归档入口，固定 verifier 镜像与容器限制。归档单测首轮因测试 UID 与只读目录权限不匹配，退出 1；修正运行 UID 后 3 项通过。提取摘要时发现宿主未安装 `jq`（退出 127），改用文本读取，未改动宿主。用该入口执行 `diagnostic 2026-11-20T05:00:00Z 2026-11-20T06:00:00Z 2026-11-20T03:52:00Z` 退出 0，归档 68/68 分钟，`sha256sum -c --quiet sha256sums.txt` 退出 0。窗前诊断结果 SHA-256 为 `28dc70335a99d9dd953fc12e187d37ecbad96b20a46f65529df1ca3cd3e1e16b`。固定工具清单 SHA-256 为 `74b6219bc3627f5fe14a54e68968e9508d99ac13565392b58513c5d694786ea4`，备用私有包为 `holdout-bundles/runner-20261122.tar`。
- T3 `schedule_task` 已创建并以 `list_scheduled_tasks` 读回本线程四个一次性任务，全部启用。前三周的固定样本和结果截止分别为 11 月 27/28 日、12 月 4/5 日、12 月 11/12 日 05:00 UTC；最终样本截至 12 月 18 日 05:00 UTC，版本化结果截至 12 月 21 日 05:00 UTC，四次运行均在相应结果截止后 10 分钟。选择周检延后 24 小时，终验延后 72 小时，保持原策略及样本窗。
- `gh api graphql` 逐帖回复及回读均退出 0，已回填 #80、#81、#85、#86、#92。现运行项目容器身份与阶段基线 `diff` 退出 0，分钟定时器 active，磁盘约余 22 GiB。下一步同步研究分支；到期自动按冻结评估器继续核验外部参考、成本和样本边界。 [复核数据与讨论链接](../../reports/profitability-experiments-results-20261122.md#原生事件复核与自动跟进)

- 10:18 UTC，`git diff --check 659c0b84..HEAD`、`git push origin HEAD:refs/heads/fix/TASK-78-hermes-telegram-sync`、`git ls-remote --exit-code --heads origin refs/heads/fix/TASK-78-hermes-telegram-sync` 均退出 0，已核对远端成果提交 `a4ec1de7c84a32a78accaf0fcdd857c81861ceae`。当前等待首周样本截止及结算成熟，由已启用的一次性任务自动继续。自动任务的本地工具入口与私有备用包都须用固定清单校验。

## 10:23 UTC · 止损策略快速回放

- 新增 stop_loss.py 和 4 项边界测试。回放按每次入场前已观察到的最新结算版本决策，修订回执只保留最新可见版本，未把未来回执泄漏给止损规则。
- run-isolated.sh 运行两次退出 0，cmp 退出 0，结果 SHA-256 为 46342cedaa423fe75c399dde2bf4a8fe905aeb7baea6066a02b594c9f4de1913。154 项盈利相关回归通过，2 项数据库测试按既有条件跳过。
- 基线 453 笔压力收益为 -48.268。累计回撤 2 单位熔断为 -3.635（22 笔），连续 3 次亏损熔断为 -3.875（31 笔），同场 1 笔加每日亏损 2 单位熔断为 -4.392（77 笔）。费用和币种门禁拦截 453 笔，因为当前没有合格费用证据。
- 结论只用于止损研究。累计回撤和连续亏损熔断能快速缩小损失，但开放头寸会在熔断后继续结算，任何规则都没有显示费后盈利。未修改生产开关、账户限额和执行器。
- 复现报告见 reports/profitability-stop-loss-20261122.md，下一步把候选止损规则放入四周纸面窗，只做无资金验证。

## 2026-11-26 03:17 UTC · 长期止损与证据审查继续

- 用户明确要求本项目的盈利、研究与验证持续进行，直至用户确认完成，并区分确定性与不确定性。活动阶段保持原四周登记与验收阈值不变，候选止损仅进入独立纸面验证，不连接真实发单流程。
- 容量风险处置：根分区此前可用 1.4 GiB，Docker buildx du 显示 21.76 GiB 可回收缓存。用户先前已明确授权磁盘不足时清理 Docker 构建缓存。docker buildx prune --all --force 退出 0，缓存现为 0B，df -h . 可用 22 GiB。未删镜像、卷或容器。项目容器 ID 与本任务基线逐一 diff 退出 0。原注册 verifier 摘要当前 daemon 不存在，故使用固定应用镜像 sha256:b3910d921ed6fcb2968129aa43bac0803895de46ace2881351ea3e623ee6d93c，并加研究标签 ai-football-profitability-runtime:20261126。镜像不含凭据环境变量，研究执行不注入生产配置。
- 修正止损证据时序：只有已结且当时可见的最新有效回执影响损益、连续亏损及同场释放。453 笔里有 1 笔多版本有效回执，最新观察版本唯一，没有并列最新时刻。上一版把 8 条没有有效已结回执的记录计入收益，已修正并在报告显式记录。
- 正式入口第一次复跑 run-isolated.sh 47b80e1 退出 125，因为原固定镜像摘要不存在。更新统一研究入口与 README 到当前固定摘要后，源码冻结于 2470b2be。test_profitability_stop_loss.py 5 项通过，结果独立运行两次 cmp 退出 0，输出摘要 ae01ae89866ca14909d05ef6e90ff8e48d421758435270106d622d226dcdc66d。盈利相关回归 155 项通过，2 项因数据库隔离条件跳过，退出 0。
- 使用已结回执后，基线 453 个已受理事件中 437 个有有效结算，压力单位收益 -48.108。累计回撤 2 单位规则保留 22 笔、-3.635；3 连亏熔断保留 31 笔、-3.875；同场 1 笔与每日 2 单位止损组合保留 77 笔、-4.332。各组仍为负且费用、币种未核实。旧结果不再引用。
- 03:17 UTC 前，首周样本范围应有 8,537 分钟档，存在 8,525 个有效 JSON 档，缺 12；1,244 条建议中有 8 条完整原生预检、4 条预检不可用。拒绝原因：盘口 760、比分 259、来源 113、超期 76、合同 17、预算 5、赛事未开 2。审批合同 0，费用凭证 0。采样量和拒绝码是确定计数，后续盈利未知。
- 五个周检/验收任务已更新为新镜像摘要与新归档清单哈希 9a5d1dafbedd148be9a18107afdfe7b854e6b30ea80f9a7324a54528a7404eff，周检执行日期及结果截止维持原登记。另启用 12 月 28 日的冻结窗后续任务。若用户未确认，下一任务继续核验；即使盈利门槛已满足，也报告状态并等待用户明确确认完成后才停止。
- Pi 独立审计已用指定 DeepSeek V4.1 Flash 配置启动，当前仍运行。Pi provider 必须显式设为 pi，否则 Codex provider 不接受这个模型 ID。按 T3 completion wake 约束不轮询子任务，完成后继续吸收审计意见。
- 关联讨论将在当前阶段确认后更新。[止损回放及确定性/不确定性拆分](../../reports/profitability-stop-loss-20261122.md)

- 止损结果与当前窗口状态已回填到 #80、#81、#84、#85、#86、#92。第一次尝试回复已有回复子链的 comment 被 GitHub 拒绝，退出 1 且未创建回帖。改为对应 Discussion 顶层评论后 GraphQL 退出 0，五个新 comment URL 均读取验证退出 0。
- 本轮启用五个 T3 一次性检查：11 月 28 日、12 月 5 日、12 月 12 日、12 月 21 日、12 月 28 日 05:10 UTC，列表读回全部 enabled。前四次执行原登记周检与终验，12 月 28 日继续未完成目标并排定后续检查，直到用户确认或满足预先设定的盈利门槛。
- [止损回放及确定性/不确定性拆分](../../reports/profitability-stop-loss-20261122.md)

- 03:25 UTC，远端推送前完整区间 diff-check 首次因新增止损测试文件末尾空行退出 2。仅去除该空行，最终固定提交 13d9a99b 在统一无网络入口重跑 5 项止损测试和 155 项盈利相关测试全部通过，2 项按数据库隔离条件跳过。两次隔离回放 cmp 退出 0，SHA-256 均为 ae01ae89866ca14909d05ef6e90ff8e48d421758435270106d622d226dcdc66d。
- 03:25 UTC，git diff --check 659c0b84..HEAD、git push 和 ls-remote 核验均退出 0。远端任务分支现为 13d9a99ba641b8327b976433bb8e8c500c6cb478。项目 Docker 构建缓存已清至 0B，可用空间约 22 GiB，生产及项目容器 ID 对照无变化。首周纸面采样 timer active，首个周检定于 11 月 28 日 05:10 UTC。该长期任务仍进行中。

## 2026-11-26 03:30 UTC · 定价链首轮具体阻断已分解

- 将首周已有 1,244 条前瞻建议、8 条完整原生预检与 4 条限额拒绝在隔离容器内只计数复核。8/8 完整预检的独立参考定价拒绝原因均为 reference_contract_missing；4/4 预检失败为 account_stake_outside_available_limits。命令用固定 Python 摘要镜像、断网、只读挂载输入，退出 0，只输出聚合。
- 明确下一步的可确定动作是补验证赛事、盘口、期间及滚球比分的参考合同映射，并查明费用凭证入口。降低订单金额是否可执行、参考价格和成本之后是否为正边际仍未知，当前不以试错方式改用户配置或发送更多账户请求。
- 这 12 条不是盈利样本。所有参考匹配和费用目前 0 批准、0 证据。具体阻断已补入盈利/止损报告并将更新对应讨论。

- 03:31 UTC，进一步对 8 条完整原生预检按期间/玩法聚合：全部是足球滚球大小球，半场 4 条、全场 4 条。当前 14 场参考清单中能匹配这 8 场的数量为 0；独立定价的 8 个精确拒绝码均为 reference_contract_missing。另 4 条预检因采样金额不在当时可用限额而失败。金额调整、外部价差和费用后边际都未知。报告已更新，#81 和 #92 新状态评论已通过 GraphQL 创建并回读。

## 2026-11-26 03:32 UTC · 参考价格与止损样本分母已核清

- 1,244 条候选里 8 条完整原生预检全部为足球滚球大小球，半场/全场各 4 条。每条独立参考定价都返回 reference_contract_missing。参考清单 14 场，匹配这 8 场为 0。其余 4 条原生预检失败原因为 account_stake_outside_available_limits。金额配置降低后的价格、钱包余量及正边际未知；既没有降低任何真钱参数，也未再次发起账户查询。
- GitHub 当前聚合进度已回填至 #81、#85、#92，Discussion URL 回读退出 0。条目说明这 12 条仅构成定价与限额覆盖诊断，不能用于推断收益。
- 五个 T3 一次性后续任务的 prompt 都已更新为只有用户明确确认完成后才能停止。若预设盈利门槛达到，也必须报告结果并继续等待明确确认。下一运行日期不变。

## 2026-11-26 03:45 UTC · 外部盘口方案完成官方文档筛查

- 依据官方资料核验 Football-Data、The Odds API、Betfair Exchange 与 Polymarket CLOB 作为参考源的技术边界。研究记录见 reports/profitability-data-provider-gates-20261126.md，含确定性事实和未验证项。
- 当前 8 条完整原生 preflight 均为滚球 totals，当前 14 场参考清单与 8 场交集 0。独立定价 8/8 缺精确合同匹配；费用凭证仍 0。
- The Odds API 文档描述 /odds 可含 live/upcoming，标准 totals 固定赔率滚球刷新为 40 秒，慢于本系统 15 秒要求。Exchange 类市场标称 10 秒间隔，但所需赛事覆盖、入库延迟、深度未知。历史 Odds API snapshots 为 5 分钟，不能补齐15秒时点价。Football-Data最新fixtures odds按周采，适合历史收盘基线，不能当滚球源。
- The Odds API官方价目页标明免费500 credits/月，20,000 credits档当前30美元/月。商业应用可使用其报价，但原始feed不可单独转售。仅文档研究，没有购买、注册、请求新密钥或访问受限账户。
- Betfair 官方 Exchange API 有价格、成交量和低延迟 Stream API，现有样本映射与费用未证。Polymarket CLOB 提供二元 YES/NO 当前orderbook，官方说明显示价可以是中间价且开赛时自动撤单，不能将其视为滚球大小球直接替代价。
- 阶段决策：不购买赔率API。只有先对新原生样本完成精确合同匹配、timestamp不超过15秒、全选项深度、费用和币种证明后，才纳入费后盈利计算。报告和官方链接已入库。
- 当前运行的 API 和 reference-price 容器只读核对环境变量名与挂载类别，未发现 The Odds API / Betfair / Polymarket 的接入键。价格容器实际挂载仅含本地reference data、已有public orderbook/score archives及Unix socket，没有读/输出密钥值。范围仅涵盖这两个当前运行容器，不能外推外部账号。

## 2026-11-26 04:32 UTC · 独立止损审计修复与重算

- 目标继续以缩小可确定亏损、厘清不确定因素并验证持续盈利条件为主。本阶段只在隔离历史/纸面回放内工作，未改生产配置、账户限额或发单路径。活动工作包为止损时间语义与压力口径审计，依赖为冻结受理档和已有隔离容器。
- Pi 的 16 组独立审计意见逐项核对。修复了修订后余额/回撤重建、同观察时刻批处理、截止后订单过滤、无永久熔断锁存、重复 `order_id` 拒绝、畸形输入错误类型和区间校验。统一两套模拟器的最新有效已结回执选择，未结回执不再进入收益分母。费用只在币种也可核实时扣除，走盘不扣固定压力成本或形成连亏。
- 审计中另有策略口径误读。`fixture_1_daily_loss_2` 的配置是同场最多 1 笔、每日已结亏损 2 单位。探针预期后来赢钱订单能被追溯恢复，但历史时点已经拦截的订单不可回填；修订只影响后续决策。每日止损明确按 UTC 结算观察日统计，因为损失在该时点才可见。按下单日风险预算保留为独立待研设计。
- 本轮再次发现注册策略模拟器和止损回放对 0.02 压力成本定义不同。已统一为非走盘回报扣固定 0.02，费用和币种同时可核实时扣费。当前 36348cc7 首次目标测试有 2 个断言仍沿用旧压力成本回撤值，退出 1。修正测试预期后，73ffbf0f 的止损测试 23 项通过、策略模拟测试 6 项通过，盈利相关测试 174 项通过，其中 2 项依既有数据库隔离条件跳过。失败与修复链均保留。
- 最终固定源码 `73ffbf0f27c45539210dab4896b75de34bc9f864` 在固定镜像、断网只读容器中对冻结输入运行两次，`cmp` 退出 0，JSON 摘要 SHA-256 为 `ec4aaa59097886f2f2f9b4b0321b7cc9d5bb840412af66b356ba523d1d7d3f70`。基线 453 笔中 437 笔已结，费用或币种仍全部缺失；基线压力单位收益为 -47.888。最大回撤 2 单位留下 22 笔、-3.635；每日已结亏损 1 单位留下 234 笔、-19.563；每日已结亏损 2 单位留下 300 笔、-24.446。每个可计收益方案仍为负，且被拦机会后果未知，不能据此宣布止损方案盈利。
- 报告 `reports/profitability-stop-loss-20261122.md` 已替换旧表格并补入 Pi 审计逐项复核、明确日亏损口径及失败记录。下一步更新远端讨论，推送这批经验证结果，随后继续前瞻样本的价格、费用和现金风险证据审查。

- 04:35 UTC，`git diff --check origin/fix/TASK-78-hermes-telegram-sync..HEAD` 退出 0；`git push origin HEAD:refs/heads/fix/TASK-78-hermes-telegram-sync` 退出 0，`git ls-remote` 核对远端提交为 `380b83ec78ea3595119d62b17174f3153e5cb88c`。GitHub Discussion #84 更新见 https://github.com/1420970597/ai_football/discussions/84#discussioncomment-19911159，#85 更新见 https://github.com/1420970597/ai_football/discussions/85#discussioncomment-19911164；创建后分别回读确认，GraphQL 查询均退出 0。五个定期/终验任务状态回读全部 enabled，下一次 11 月 28 日 05:10 UTC。

## 2026-11-26 04:45 UTC · 延伸核验容量、敞口与样本区间

- 目标检查止损触发后仍在途的结算及现金峰值，并纠正任何继续使用未结回执的资金结果。已复用 C04/C05 原有隔离模块，没有另建影子实现。
- 发现 C04/C05 的 `_valid` 只检查收益字段和观察时点，没有要求回执已结；同刻冲突版本还会按输入列表挑选。修改为只纳入有效已结且截止可见版本，最新观察时点若存在冲突则作为歧义排除。C05 的资金本金仍只读源入场字段，不从回执倒推。
- 隔离容器中 `test_profitability_experiments_risk.py` 10 项通过，完整盈利相关回归 177 项通过 175 项、2 项因数据库隔离条件跳过，退出 0。覆盖未结回执、后续未结修订及同刻冲突版本。
- 对同一冻结输入的 C04/C05/C06/P06 汇总回放双次运行，`cmp` 退出 0，摘要 SHA-256 为 `cca2bc75189e91829e59e98989f1ba169384dbf45296796155769e3d7e8a230f`。C04 453 笔有 437 笔有效已结，16 笔没有结果；已结组费用前单位收益 -39.368，费用/币种证据均缺。C05 同场 1/2 开放头寸分别拦截 137/35，开放头寸峰值从 43 降至 28/38；453 笔的入场本金仍全部未知。
- C06 的 437 笔压力 ROI 为 -10.958%，按赛事区块 95% 下界 -19.324%；278 个赛事，只有 2 个完整周且仅 1 周为正，16 笔未结。P06 按赛事设计效应 1.168、有效样本约 374，赛事区块 95% 区间 -19.37% 至 -2.22%，两日区块 -16.51% 至 -4.77%。这些是同一历史样本的可复现负向估计，不是未来保证。
- `reports/profitability-experiments-results-20261122.md` 与止损报告已更新 C04/C05/C06/P06 结果。资金风险回执口径修正已进入讨论回填，待报告提交推送后回到仍启用的前瞻窗口检查。
- 04:48 UTC，#83/#84/#85/#91 更新分别为 https://github.com/1420970597/ai_football/discussions/83#discussioncomment-19911356、https://github.com/1420970597/ai_football/discussions/84#discussioncomment-19911359、https://github.com/1420970597/ai_football/discussions/85#discussioncomment-19911357、https://github.com/1420970597/ai_football/discussions/91#discussioncomment-19911358。四个 GraphQL 创建调用退出 0，别名回读退出 0。初次用同字段不同参数的回读查询触发 fieldConflict 并退出 1，改用别名后验证成功。止损单项结果在最终提交 7a9d0faa 下再次双跑，`cmp` 退出 0，SHA-256 仍为 `ec4aaa59097886f2f2f9b4b0321b7cc9d5bb840412af66b356ba523d1d7d3f70`。下一步提交和推送报告/讨论回填，再按 11 月 28 日 05:10 UTC 前瞻检查继续。
- 04:52 UTC，补测发现同一观察时刻的两份回执即使单位收益相同，只要 stake 不同也会改变 C04/C05 金额，故必须按原始回执行一起判断歧义。`c05b1692` 已拒绝该并列金额，不把列表顺序用于挑选。风险测试 11 项通过，完整盈利相关测试 178 项通过 176 项、2 项因数据库隔离跳过，退出 0。C04/C05/C06/P06 双跑 SHA-256 仍为 `cca2bc75189e91829e59e98989f1ba169384dbf45296796155769e3d7e8a230f`。止损最终提交 `c05b1692` 双跑结果仍为 `ec4aaa59097886f2f2f9b4b0321b7cc9d5bb840412af66b356ba523d1d7d3f70`，cmp 退出 0。下一步更正文档测试计数、推送并核对远端。
- Discussion #85 原测试计数 177/175 已编辑为 178/176，`updateDiscussionComment` 与回读查询均退出 0，沿用原 comment URL。下一步提交、推送当前研究分支并回读远端 SHA。
- 04:57 UTC，冻结报告与修复提交 `199d9c624ca2dfb0260c49c07610f68f4b86674a` 已推送。`git diff --check origin/fix/TASK-78-hermes-telegram-sync..HEAD` 退出 0，push 退出 0，`git ls-remote` 读回同一 SHA。一次宿主 `jq` 摘要命令因未安装以退出 127 结束，改用固定 Docker 镜像内 Python 只读聚合，结果和 SHA 随后双跑核验。远端当前研究结论为止损降低了样本内总损失但没有产生正 ROI。活动工作包转回前瞻原生价格、费用和资金边界；原四周窗保持不变，下一次定时检查仍为 11 月 28 日 05:10 UTC。
- 05:08 UTC，原生赔率劣化回放结果已更新至 #81 `https://github.com/1420970597/ai_football/discussions/81#discussioncomment-19911655`，#84 原回帖已编辑为最终结果 `https://github.com/1420970597/ai_football/discussions/84#discussioncomment-19911159`，#85 测试计数更新为 179/177 `https://github.com/1420970597/ai_football/discussions/85#discussioncomment-19911357`。调用或编辑和别名回读均退出 0。源码提交 `a40740038449fae9d39b60c288e906dc3bd78108`，隔离止损测试 24 项通过、全量 179 项中 177 项通过、2 项按隔离条件跳过；两次回放 SHA-256 `bd79bce983c1defa47d8f038788b0100d5b66fc039e0e60d042d7d0c5d46711e`。下一步更新报告推送，继续保持原前瞻登记门槛。
- 05:09 UTC，报告提交 `3aeb40e6a2de042a38fefacb6c014b1f5a0e7111` 的 `git diff --check` 退出 0，推送退出 0，`git ls-remote` 核对分支 SHA 为 `3aeb40e6a2de042a38fefacb6c014b1f5a0e7111`。价格劣化诊断的确定性结论为拦掉 48 笔后仍亏损；费用、过度率、同合同第二源和被拦后果仍不确定。下一阶段继续前瞻等待并核验新数据，不修改已登记阈值。
- 05:09 UTC，最新进度与已验证报告推送后，远端任务分支读回为 `b57cd055bd3b3dcbab64ad1ca481b51d1eabb61b`。`git diff --check origin/fix/TASK-78-hermes-telegram-sync..HEAD` 退出 0，任务涉及文件状态干净。五个自动后续任务仍 enabled，下一次首周检查在 11 月 28 日 05:10 UTC；本项目中其他既有脏文件均保留未动。继续目标为按原样本窗检查独立价格合同、费用凭证和风险边界，用户未确认完成前不停止。
- 05:09 UTC，进度尾注提交后 push 和远端 `ls-remote` 均退出 0，分支现为 `1910aceaf64c1d4b02d6d436d621895e9fa8201e`。当前阶段交付已同步，长期验收与自动检查继续。

## 2026-11-26 08:05 UTC · 联赛表现差异与比赛操纵假设初筛

- 目标：检验专家结果是否随联赛/赛事分层变化，并判断亏损数据是否能支持比赛操纵假设。分析只读冻结受理订单及只读 API 数据库投影，没有改自动投注配置、生产代码或账户行为。
- 结果队列：窗口 `2026-11-02T00:00:00Z` 至 `2026-11-20T03:00:00Z`，截止 `03:20Z`；453 笔已受理单、288 场、437 笔已结、16 笔未结、118 个显示名称分组；437 笔均缺费用和币种。总体压力单位 ROI −10.96%。让球 115 笔为 −22.63%，大小球 322 笔为 −6.79%。该差异可以由玩法构成影响联赛聚合值。
- 映射审计修正：订单档不含联赛字段。按 `fixture_id` 连接的只读来源明确为 2026-11-26 当前赛事投影，288 行 `competition_id` 全部为 null。因此结果只能称为“按当前赛事显示名称分组”，不能视为下单时联赛 ID。报告已标注此限制，持久化层应在推荐时保存不可变赛事 ID 与名称快照。
- 局部负向信号：巴西甲级联赛显示名称组 15 笔、7 场、5 位专家，压力 ROI −40.8%，6/7 场的平均压力收益为负；组内只有 2 位专家满足同专家配对最低样本。对 118 组调整后，穷举 7 个赛事簇的全部 823,543 个有序区块重采样结果（折叠为 1,716 个组合），familywise 区间约 −94.86% 至 −1.71%。精确枚举消除了蒙特卡洛尾部噪声，但 7 场仍不足以确认稳定分层效果，且不用于判定操纵。澳大利亚锦标赛 16 笔/5 场 ROI −50.7%、东南亚球会锦标赛 14 笔/6 场 ROI −38.1%，相应 familywise 区间均跨零。
- 确定/不确定边界：确定的是冻结队列存在组间回报差异、全样本为负、费用币种未知且市场组合不同。可能的解释包括专家组成、赛事信息质量、市场效率、盘口构成、被接受样本选择和赛果方差，当前无法区分。没有本样本比赛对应的完整性预警、投注量/流动性轨迹、账户信息或赛事现场证据，因此不能从收益异常推断“某联赛严重假赛”。公开官方资料记录了巴西 2022–2023 年特定赛事的操纵案件，MPGO 于 2025 年通报首批三名被告判刑；该历史案件与本次 2026 年回放没有比赛级连接，不能外推到整个联赛。
- 实现与提交：`e03ccfad` 补入联赛 ID 覆盖字段，对 5–7 个赛事簇精确枚举 familywise 区间，并添加两个边界测试。修改仅涉及 `competition_analysis.py` 和对应测试。
- 隔离验证：定向命令 `bash infra/profitability/experiments/run-isolated.sh e03ccfad .runtime/profitability-experiments-20261122/input .runtime/profitability-experiments-20261122/public -- -m unittest discover -s tests -p 'test_profitability_experiments_competition_analysis.py' -v` 退出 0，6/6 通过。完整命令 `... -- -m unittest discover -s tests -p 'test_profitability*.py'` 退出 0，185 项中 183 通过、2 项按既有数据库隔离条件跳过。
- 真实归档命令在固定镜像、`--network none --read-only --cpus .5 --memory 384m` 容器运行两次：`bash infra/profitability/experiments/run-isolated.sh e03ccfad .runtime/profitability-experiments-20261122/input .runtime/profitability-experiments-20261122/public -- -m infra.profitability.experiments.competition_analysis /input/native-cohort.json /input/league-fixture-metadata.json --bootstrap-rounds 100000`。两份输出 `cmp` 退出 0，更新后结果 SHA-256 为 `80b1068e917dfd338a8d6d2426463725cb9f67825cc42627e472d5075edcb89c`。
- 产物：`reports/profitability-league-integrity-20261126.md`。下一步更新 C06 远程讨论与结果索引，并将本阶段报告推送到已有任务分支；之后继续前瞻窗口，验证时间固定的联赛 ID、被拒绝机会分母、同合同市场报价和费用凭证。
- 08:17 UTC，首次 `git diff --cached --check` 退出 2，原因是报告元信息两行有 Markdown 换行尾空格。删除尾空格后同一检查退出 0，提交 `b058eeef` 包含代码、测试、报告、索引和进度。`git push origin HEAD:refs/heads/fix/TASK-78-hermes-telegram-sync` 退出 0，`git ls-remote` 核对远端 SHA `b058eeef5bddade78d5761515841e7bacde7c24a`。
- C06 Discussion #85 已新增联赛分层评论，`gh api graphql` 写入及按 comment ID 回读均退出 0，评论地址 https://github.com/1420970597/ai_football/discussions/85#discussioncomment-19915363。评论内容披露赛事 ID 全空、费用缺失、样本小及“收益筛查不等于操纵证据”。

## 2026-11-26 05:03 UTC · 原生预检价格劣化规则回放

- 目标区分价格质量门禁的证据覆盖失败与实际赔率变差。固定输入内 453 笔的价格字段和时钟齐全；预检价高于推荐价 93 笔、相同 312 笔、低于推荐价 48 笔。价差中位数 0，P90 +0.02，最小 -0.26、最大 +0.17。过度率缺失 450 笔，另 3 笔超出 0–0.05，严格价格质量通过 0 笔。
- 新增隔离对照 `reject_native_price_degradation_only`，仅排除预检赔率低于推荐价或必要字段/时间无效的订单，不伪称通过双源、过度率或费用门槛。实际回放选中 405、拦截 48，392 笔有效已结，压力总额 -42.144、ROI -10.751%、最大可见回撤 46.439。基线为 -47.888、-10.958%。只减少 5.744 个压力单位损失，单位 ROI 改善约 0.207 个百分点，仍负。不能视为稳定盈利，也不衡量被拦样本后续赛果。
- 代码提交 `a40740038449fae9d39b60c288e906dc3bd78108`。24 项止损测试通过，6 项策略模拟、11 项资金风险测试通过；完整盈利相关回归 179 项通过 177 项，2 项依数据库隔离条件跳过，退出 0。两次隔离实档回放 `cmp` 退出 0，结果 SHA-256 `bd79bce983c1defa47d8f038788b0100d5b66fc039e0e60d042d7d0c5d46711e`。
- 止损报告与总览已加新规则、赔率差分分布、严格门禁缺口。下一步把这项有限止损收益回填 #81/#84，更新 #85 测试计数，推送并核对远端 SHA。后续原登记仍按 11 月 28 日 05:10 UTC 周检，期间不改登记窗和门槛。
