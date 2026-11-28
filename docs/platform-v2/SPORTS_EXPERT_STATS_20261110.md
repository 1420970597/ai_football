# 足球与篮球专家统计分离

更新时间：2026-11-10 14:32 UTC。责任包：sports_stats_ui；主线仍为专家原文抽取准确率。

目标：专家页面按足球、篮球独立查看正确率、有效结算、一单位净收益及走势；最新推荐使用同一运动筛选，不能用足球统计配篮球推荐。责任目录为 services/api 统计及公共推荐接口、web/app 和针对性 tests。前置依赖为已入库赛事 sport、不可变报价证据、最新结算台账及当前统计重置周期；不增加迁移或写生产数据。

当前证据：expert_statistics.signal_scope 没有 sport 条件，同专家足球及篮球共用分母；专家页只有窗口、阶段、玩法、来源筛选；详情及走势复用同 scope，但最新推荐不接收运动筛选。赛事表已有 sport，公共推荐响应已有 f.sport，可复用事实字段。

设计：StatisticsScope 与无 sport 的旧公共 API 继续默认 all，保留既有汇总语义以及盈利专家自动跟随资格、统计重置预览；页面独立 sport 选择默认 football，允许 basketball，显式发送给榜单、详情、走势、来源和最新推荐。公共统计接受 all/football/basketball，拒绝未知值；推荐分页令牌绑定 sport，all 保持历史游标范围。统计只在现有质量/周期条件前增加赛事 sport 范围，不改算法、旧单或收益证据。不把 UI 筛选变成投注许可或盈利资格修改。

实现动作：扩展现有 scope 与公开推荐查询，新增页面运动筛选和清晰范围标签，处理切换时的迟到推荐响应；独立统计及浏览器验收覆盖足球/篮球反向输赢、半赢半输、走盘作废、待结算、缺证据、重置排除、来源/时间/阶段/玩法、趋势及游标跨运动拒绝。统计准确率仍按有效份额加权，样本不足显示空值。

验收：隔离数据库比较同一专家足球与篮球统计得到不同正确分母/净收益，all 等于两者合计且盈利资格不受 UI 默认影响；重置旧信号及赛果更正不能重新入分母；HTTP 未知 sport=422、跨 sport 游标=400，会员权限保持；桌面1440与手机390真实浏览器点击切换、详情趋势与推荐参数一致、迟到旧请求不能污染新运动结果，页面无横向溢出和脚本错误。浏览器协议样本与生产证据分别记录。

失败处理：保留原失败码与产物，修复后重跑原场景；不降低断言、填零或改用户投注开关。不提交、推送、部署，由主代理合并后冻结完整验证。

当前状态：隔离验证通过，等待主代理合并统一冻结与真实联调；未提交、推送或部署。冻结根提交 `4b8a0b34547669f14fe15f7e8d8f7a4084c284af`，独立快照 `.runtime/sports-stats-20261110/source`。此快照包含本包覆盖文件，不包含其他代理随后写入的赛前/篮球执行和篮球结算实现；相关合并回归不能由本报告替代。

已完成证据：`targeted-final.stderr` 原生退出0，12项测试全部执行；足球/篮球独立分母、收益、来源、详情走势、窗口/阶段/玩法、reset watermark、真实重置撤销、推荐分页/权益、缺失证据和中性结果覆盖。`.runtime/sports-stats-20261110/evidence/sports-expert-statistics-results.json` 两种宽度1440/390通过，原生浏览器退出0；点击切换、正确率75%与25%分离、无样本提示、详情走势、迟到足球响应不会污染篮球列表、无横向溢出和脚本错误，API写入0。浏览器为真实 Chromium，数据为显式隔离样本；本证据不代表真实篮球结算或提供方联调。

失败保留：`targeted.exit=1` 是新增测试数据重复原推荐唯一身份，修复为每种结果独立消息；`targeted-retry.exit=1` 揭露原统计重置预览固定SQL参数位置，修复后12项重跑通过。`compat.exit=1` 是隔离账户测试强制专用host alias与首次调用不符，保持测试守卫不变，改用现有受限角色及 `ai-football-v2-pgtest` 重跑。`visual-before.exit=1` 因 `--network none` 导致浏览器认为离线，比分页面未发请求；保持前端离线逻辑不变，使用本任务独立internal Docker网络重跑。自动网络池耗尽的原命令退出1已保留控制台事实，随后检查现有子网，明确使用未占用 `10.88.114.0/28` 创建本任务网络；不清理其他任务网络。

当前已知依赖：本包冻结基线中的 `settlement.py` 对非足球以 `sport_requires_provider_settlement` 保留待结算，已同步篮球协议责任代理；其并行实现另行验收。统计包不会猜篮球官方规则或将未结算显示为输赢，真实篮球样本仍须实际协议与赛果证据。本人投注范围文案依赛前与篮球责任代理的执行能力同步为足球赛前/滚球、篮球全场胜负/大小/让分赛前/滚球；本人启用、专家、金额与限额确认保留。`profitable-experts.mjs` 的新执行能力样本及两张控件基线由赛前责任代理更新，本包只更新一般视觉基线中的专家页两张。

补充修复：原榜单标题为“有效结算”，实际读取含走盘/作废的 `settled_signals`。现改读 `effective_settled_signals`，仅字段不存在且全赢/半赢/全输/半输四项均为非负整数时按分类合计兼容旧响应。明确的null显示样本不足，明确的0保持0；条数与用于准确率的加权风险份额分别处理。专项浏览器加入仅走盘作废、半赢半输两条/有效份额1、旧分类响应、明确null与0的断言。详情仍称“已结算推荐”，并逐项展示走盘和作废。

最终证据根目录为 `.runtime/sports-stats-20261110/evidence`：

| 原生检查 | 实际结果与证据 |
| --- | --- |
| 新统计/接口及原统计子集 | `targeted-final.exit=0`；12 tests，10.217s；`targeted-final.stderr` |
| 原盈利资格、本人许可、重置与CLI | `compat-final.exit=0`；18 tests，45.918s；`compat-final.stderr` |
| 新双端真实浏览器点击 | `browser-final.exit=0`；1440/390均通过；`browser-final/sports-expert-statistics-results.json`，同目录两张截图；无浏览器错误、真实外部请求或生产写入 |
| 既有完整浏览器入口 | `browser-legacy.exit=0`；67项passed，`browser_errors=[]`；`browser-legacy/results.json`及原日志/截图 |
| 原14屏视觉第一次在线比较 | `visual-online.exit=1`；只有专家1440/390两屏预期变化，其余12屏changed_ratio为0；`visual-online/visual-results.json`及全部14原截图保留 |
| 新基线审查与最终比较 | `visual-reviewed.exit=0`；实际目视两端，桌面标签不被挤成竖排；只更新专家两张PNG和manifest对应两项SHA，原阈值channel16/ratio0.002、其他12项及所有policy不变；`visual-final.exit=0`，14屏全通过且0像素差 |
| 语法与shell | `syntax.exit=0`；Node检查app.js/新浏览器脚本，sh检查浏览器入口；Git `diff --check`原生0 |
| 冻结一致性 | `freeze-consistency.exit=0`；13个本包代码/测试/基线文件与冻结测试副本逐项SHA相等，`freeze-consistency.json` |

最终检查容器全部原生ExitCode0、OOMKilled=false；失败容器退出1如上保留，见 `container-status.txt`。总计30项后端行为测试、专项两端点击、既有67项浏览器场景及14屏视觉比较分别计数，未将截图数量算成业务测试或把模拟样本算成真实业务成功。

实际命令如下，均在项目根执行；各次stdout/stderr分别保存，随后捕获同一Docker命令返回码到上述 `.exit`。没有管道覆盖失败码。冻结源码运行期间没有修改。

```sh
docker run --name ai-football-sports-stats-20261110-tests-final --user 0:0 \
  --network ai-football-e3056-full-private --cpus .3 --memory 256m --pids-limit 96 \
  --read-only --tmpfs /tmp:rw,nosuid,size=48m -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/source/services/api:/source/tests:/source/services:/source \
  -e TEST_DATABASE_ADMIN_URL=postgresql://postgres@ai-football-v2-pgverify/postgres \
  -v "$PWD/.runtime/sports-stats-20261110/source:/source:ro" --entrypoint python \
  sha256:a8fc0cb5dbebed71df5688f33c3963a076151c7d41ca8e7625520b8ab7ffc234 \
  -m unittest -v test_sports_expert_statistics test_expert_statistics test_performance_statistics \
  test_result_ledger.ResultLedgerTests.test_settlement_time_window_filters_and_nickname_identity \
  test_result_ledger.ResultLedgerTests.test_same_nickname_cross_group_stays_distinct_and_source_options_hide_disabled
```

原兼容检查使用完全相同资源、镜像与挂载，容器名 `ai-football-sports-stats-20261110-compat-final`，另加 `-e ACCOUNT_TEST_DATABASE_URL=postgresql://account_state_test@ai-football-v2-pgtest/postgres`，末尾实际为 `-m unittest -v test_profitable_experts test_statistics_cycle_cli`。两个别名均来自现有任务隔离PostgreSQL容器，每例创建/删除本例临时库；没有生产连接。

```sh
docker run --name ai-football-sports-stats-20261110-browser-final \
  --network ai-football-sports-stats-20261110-browser-net --user 0:0 \
  --cpus .5 --memory 768m --pids-limit 128 --read-only --tmpfs /tmp:rw,nosuid,size=128m \
  -e BROWSER_EVIDENCE_DIR=/evidence/browser-final \
  -v "$PWD/.runtime/sports-stats-20261110/source:/work:ro" \
  -v "$PWD/.runtime/sports-stats-20261110/evidence:/evidence" \
  sha256:ece536b0cb81dd6dabfa43c57163750589f023219a28e7fc41e4b64e8b989a7c \
  tests/browser/sports-expert-statistics.mjs
```

原完整浏览器命令使用同一镜像/网络/只读挂载/CPU内存，容器名 `ai-football-sports-stats-20261110-browser-legacy`，pids-limit256，环境为 `BROWSER_EVIDENCE_DIR=/evidence/browser-legacy`、`BROWSER_RESULTS=/evidence/browser-legacy/results.json`，运行 `tests/browser/verify.mjs`。视觉最终命令使用专项浏览器相同资源和只读源，容器名 `ai-football-sports-stats-20261110-visual-final`，环境为 `VISUAL_RESULTS=/evidence/visual-final`，运行 `tests/browser/visual-regression.mjs`。基线生成只在独立 `visual-candidate-source` 中以 `VISUAL_BASELINE_UPDATE=reviewed` 加 `--update` 执行；经SHA差异核对只有两张专家页变化、实际目视后才将其两项复制回项目。未修改测试阈值、fixture、比较代码或其余页面基线。

冻结清单：`source-manifest.json` 覆盖1235文件，按path排序的path/NUL/SHA/newline清单哈希 `fecdbdbb449193d4a3ae8c91f1b67552b1dbcd69b03992fdccb2a06a53b31f4b`。`owned-source-sha256.txt` 给出本包13个代码/测试/基线文件各自SHA；`validation-sha256.txt` 给出最终测试/浏览器/视觉证据SHA。源码快照只读挂载，app/test镜像使用固定本地image ID，未重建或变更全局依赖。

允许主代理纳入提交的本包文件仅：`services/api/app/expert_statistics.py`、`services/api/app/expert_statistics_reset.py`、`services/api/app/routes/public.py`（本包仅signals段）、`web/app/app.js`、`web/app/index.html`、`web/app/app.css`、`tests/test_sports_expert_statistics.py`、`tests/test_performance_statistics.py`、`tests/browser/sports-expert-statistics.mjs`、`tests/browser/check.sh`、`tests/browser/visual-baselines/experts-1440.png`、`tests/browser/visual-baselines/experts-390.png`、`tests/browser/visual-baselines/manifest.json`及本文。其他代理修改、父主线进度与未跟踪reports未写入本包。

下一步：主代理统一冻结全部三个新需求工作包，按最终赛事/盘口/执行/结算契约回归并真实只读联调。当前统计隔离通过不构成篮球provider结算或实盘交易验收，也不新增、关闭或修改任何用户实盘策略。
