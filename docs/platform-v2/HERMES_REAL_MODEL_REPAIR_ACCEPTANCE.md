# Hermes 分析修复验收

记录时间：2026-10-28 06:31 UTC。业务修复提交 `0ac3603`；完整冻结及部署源码提交 `e5cc40dd587e5278360e27959dc51c9ad955d7e1`。以下是开发候选验收，不代表正式发行或真实Telegram语义准确率。

## 修复结果

- 审核领取在远端PostgreSQL约0.23秒往返下反复被5秒整体截止取消。领取整体截止改为30秒，保留单语句3秒、锁等待2秒和任务租约约束。定向测试覆盖迟缓领取可完成及卡住领取被取消。
- 消息详情原先先加载最早30条工具轨迹，最新模型输出可能在后页。新增当前最新任务、当前尝试的模型结果单独读取和直接展示；旧任务或旧尝试不补位。保留轨迹分页、秘密隐藏和HTML转义，模型分析与系统实际接受结果分别展示。
- 最终部署保留远端控制覆盖层，Telegram控制端实测HTTP200、已授权、已连接。四来源的接收检查点均持续推进。

## 真实旧模型配置验收

复用现有受保护配置中的模型名称、模型端点和提供方密钥，仅传给本任务专属Hermes。Hermes镜像固定为 `nousresearch/hermes-agent:v0.22.5@sha256:9c5561579f3ffdf333401797094ac3a743738d6c25d40f4331c7bf77b694238d`，使用项目原有中文上下文、技能和受限MCP配置。API、MCP和PostgreSQL均为临时容器；18项迁移仅作用于临时库。

执行命令（所有应用代码在Docker内运行）：

```sh
docker run --rm --network ai-football-hermes-fix-test --cpus=.75 --memory=512m --pids-limit=128 --read-only --cap-drop=ALL --security-opt=no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m -e PYTHONDONTWRITEBYTECODE=1 -e INTEGRATION_DATABASE_URL=postgresql://postgres@ai-football-v2-pgverify:5432/v2_integration_test -e HERMES_TEST_URL=http://hermes-real:8642 -e INTEGRATION_SCENARIOS=text,image,multiple,none,stale -v /root/ai_football:/source:ro -w /source -e PYTHONPATH=/source/services/api:/source ai-football-verifier:hermes-fix python tests/integration/hermes_real_model.py
```

最终退出0，五项均返回 `finish_reason=stop`。模型提供方是真实服务；足球赛程、报价、消息和图片是隔离合成样本。

| 场景 | 系统接受数 | 核验 |
| --- | --- | --- |
| 中文文字推荐 | 1 | Harbor FC 对 Mountain United，赛前全场大2.5 |
| 仅图片表达推荐 | 1 | 从原生结构化图片读取同一推荐，正文没有球队和盘口答案 |
| 两场多项推荐 | 2 | 赛前大2.5及另一场滚球小3.5，逐项比赛、盘口线和方向一致 |
| 闲聊 | 0 | 无推荐时不制造推荐 |
| 过期报价 | 0 | 已实际读取陈旧盘口，未生成可用推荐 |

所有被接受项均完成搜索、唯一消歧、读取盘口、记录推荐，工具响应证据成功。首次验收退出1是测试错误要求对已识别过期的报价再次调用记录工具；模型主动不提交是正确行为。修正为核验过期盘口已读、零接受，并在发生记录调用时要求系统明确拒绝，原五场景整体重跑通过。

私有摘要 `.runtime/hermes-real-20261028/real-model-results.json` SHA256 `da11919d4ca55b2bfebe5808f5e6daeceecdd9760b06edde211b38a0cc40d956`；测试源码SHA256 `a6c234ac30ca2b55a48b0e76c2453aa68c1d56cdaae24739fcb6cc7d567e9d87`。专属网关、API、MCP、临时PostgreSQL、内网与临时密钥副本已按归属清理。

## 冻结回归和浏览器

| 实际入口 | 退出码 | 结果与证据 |
| --- | --- | --- |
| `sh infra/test/run.sh` | 0 | 953项，零失败/错误/跳过；381.525秒，含加载383.702秒；`.runtime/test-evidence/152002/backend-tests.json`，提交e5cc40d且committed_source_verified=true |
| `sh tests/browser/check.sh` | 0 | 31项，含最新模型结果无需翻页、转义和完整工具证据；`.runtime/browser-ui-174905/results.json` |
| `sh tests/browser/integration-check.sh` | 0 | 原17场景、扩展8场景及账户/会员竞态通过；核验/刷新/回执各1、真实交易0；`.runtime/web-integration-176923` |
| `sh tests/browser/accessibility-check.sh` | 0 | 1440/390双端固定场景零已确认规则违规；`.runtime/accessibility-ui-184620/results.json` |
| `sh infra/check-runtime.sh` | 0 | Compose服务配置边界及特殊字符实际容器验证通过 |

部署后实际点击命令：

```sh
docker run --rm --network ai-football-v2_edge-private --cpus=.75 --memory=1g --pids-limit=256 -v /root/ai_football:/work:ro -v /root/ai_football/output/playwright/hermes-20261028:/evidence ai-football-browser:v2.0.0-dev .runtime/hermes-real-20261028/live-clicks.mjs
```

退出0。1440与390两宽共14组全部通过、零页面JavaScript错误：五种比分状态、日期和比赛详情、专家筛选及昵称、管理员六分区、四群目录及成员搜索、会员和个人页面。两宽均实际打开现有TG消息，页面329字符的最新模型输出与API当前尝试逐字一致，没有把原文写入结果日志。手机比分截图已目视检查；结果位于 `output/playwright/hermes-20261028/live-clicks.json`。

## 部署与运行

API、审核工作器和Web均运行 `2.0.0-dev.hermes.20261028.1`，镜像revision标签为e5cc40d。API及审核工作器的审核、阅读模块和Web app.js的容器文件SHA256与冻结源码分别一致。

| 镜像 | 本地候选清单SHA256 |
| --- | --- |
| API及审核工作器 | `f0468d8bfb32f6906a98043d9389d724971613c48499d37a413cd8ee0b42079e` |
| Web | `c02d89d815bd6b942101e9232444c2b967c4220eb47e0a9d9b238a8baba5fee8` |

构建限制为768MiB、0.5CPU。按API健康→审核工作器→Web依次部署，均退出0；API就绪200，API/Web/Hermes/隧道健康。首次API重建遗漏既有远端覆盖层，联调发现后已恢复 `infra/remote/compose.local.yml`，最终控制端真实200且已连接。远端collector/executor没有重启，原始业务数据没有清理，实盘开关保持关闭。

06:29:40 UTC只读核验：最近20分钟59次模型输出及59个已完成审核；最新模型结果06:29:32、完成时间06:29:34。四启用来源的检查点均在5分钟内推进，七位启用专家无编号占位，过期运行租约0。新的审核工作器日志未见调度超时或审核失败。私有聚合证据为 `.runtime/hermes-real-20261028/runtime-final.json`。

当前大量原文属于闲聊、推广和历史档案，零推荐不能单独说明模型失效；历史积压继续由有界队列处理。两群受Telegram成员枚举权限限制，界面明确显示原因。真实TG人工标注集、长期模型质量、受控Android设备和正式签名发行仍需各自验收，本轮不将隔离样本或网页成功扩大为这些结论。
