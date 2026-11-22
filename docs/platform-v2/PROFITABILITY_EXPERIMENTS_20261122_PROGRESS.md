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
