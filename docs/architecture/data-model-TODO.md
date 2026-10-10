# TASK-32 开发与验收 TODO

用户要求：**先完全开发，再统一测试**。下表按开发依赖推进；前期试跑不作为最终验收，最终验收必须在全部代码冻结后执行。

| 编号 | 工作 | 具体完成条件 | 状态 |
| --- | --- | --- | --- |
| D1 | 页面与接口契约 | 主要导航增加“数据与模型”；GET 返回模型、训练、前瞻表现、数据与硬盘缓存 | 开发完成 |
| D2 | 模型信息 | 显示实际更新时间、版本、参数量、有效比赛/决策、回测和前瞻指标；未训练不用假数字 | 开发完成 |
| D3 | 永久统计 | 分类文件数、逻辑字节数/实际占用、台账决策/比赛数、硬盘总/用/余；后台统计，GET不扫描 | 开发完成 |
| D4 | 三级设置 | 一级分类→二级子菜单→三级配置组；仅显示当前组；跨组草稿保留、全量保存和错误定位 | 开发完成 |
| D5 | 训练配置 | 周期按新增已结算比赛数；严格整型/范围校验；自动训练开关、CPU核数、内存MiB持久化 | 开发完成 |
| D6 | 多盘口样本 | 同场多盘口/线/方向、多个算法、多个决策时间点；同时间同行情算法概率组合，不能每场只取一个 | 开发完成 |
| D7 | 输入时间边界 | 冻结输入捕获时间；走势只到该截点；排除未来报价；旧缺失走势明确标记，不用赛后补齐 | 开发完成 |
| D8 | 标签与分组 | 分盘口实际赛果结算；半场必须有半场比分；同场不能跨训练/验证；训练标签先于验证输入 | 开发完成 |
| D9 | 训练任务完整性 | 受限独立进程；父进程持续写入样本，不被训练阻塞；只允许一个训练任务；配置变化/关闭/退出可终止 | 开发完成 |
| D10 | 模型版本可靠性 | 原子写入与切换；失败保留旧模型；全部版本、样本、观察预测永久保留；重启不会重复按同批比赛训练 | 开发完成 |
| D11 | 404兼容 | 旧后端明确提示缺少接口并停止自动404轮询；手动刷新可恢复；最终新镜像提供真实端点 | 开发完成 |
| D12 | 502排查与恢复 | 依据nginx/后端日志判断原因；前端避免并行重复请求，对临时网关失败退避并保留旧结果 | 开发完成 |
| D13 | 部署一致性 | 记录前端bind mount即时变化、后端镜像需重建的事实；提供前后端一致启动命令，用户自行启动 | 开发完成 |
| D14 | 文档 | 架构Mermaid、输入/标签边界表、三级菜单线框图、五种页面状态；回填限制与证据 | 开发完成 |
| R1 | 冻结开发 | D1–D14全部审查完成后停止改写源码，记录文件哈希；转入验证阶段 | 已完成，开始统一验证 |
| V1 | 语法与单元测试 | Python编译、JS语法；新增模块正常/异常；完整unittest经cpu-limited.sh执行，记录退出码 | 已通过，退出码0 |
| V2 | 类型与lint | 容器Python3.12中mypy、ruff全绿，记录退出码 | 已通过，退出码0 |
| V3 | 浏览器交互 | 主页面回归；三级菜单草稿/全量提交/错误定位；未训练/训练失败/404/502；桌面及320/390px | 已通过，退出码0 |
| V4 | 隔离集成 | 临时数据卷，受限训练进程实际核数/内存；API端点、启动/关闭、周期/重启、模型失败保护 | 已通过，退出码0 |
| V5 | 镜像与交付 | cpu-limited.sh构建；隔离镜像健康验证；不连接场馆、不替用户部署；提交并创建PR | 已完成，PR #44（draft） |

## 当前生产报错证据（2026-10-10 UTC）

- 后端生产镜像为 `a62c520c0603`，08:19:56启动；前端nginx从03:59:03运行，静态文件挂载本地 `web/console`。
- `/data-model` 的404：新增前端已经可见，但生产旧后端还没有新端点；与浏览器网络无关。
- `/recommendations` 的502：nginx在07:05:19、07:39:28、08:01:05记录 `connect() failed (111: Connection refused)`；08:19:55记录 `upstream prematurely closed connection`，紧邻后端08:19:56启动。这些记录表明后端端口短暂不可用/连接被关闭，不是用户网络问题。
- 本次日志读取时三容器healthy、后端RestartCount=0；RestartCount=0只能说明**当前容器**没有自动重启，不能否认曾重建容器。没有证据将错误归因于新训练进程（生产镜像尚未包含它）。

## 最终统一验证（2026-10-10 UTC，源码冻结）

以下均真实执行，**退出码0**。验证后业务源码哈希保持一致；仅本交付记录追加。

| 验证 | 命令/方式 | 实际结果 |
| --- | --- | --- |
| Python编译 | `./scripts/cpu-limited.sh run -- python3 -m py_compile` 后接11个新增/修改Python文件；另用ast解析77个Python文件 | 无语法错误 |
| JS语法 | `node --check web/console/workbench.js`，以及两份浏览器流程文件 | 无语法错误 |
| 全量unittest | `./scripts/cpu-limited.sh run -- python3 -m unittest discover -s tests -q` | 1246测试，11跳过，最终46.138秒 |
| Python3.12回归 | `docker run --rm --cpus=2 -v /root/ai_football:/w:ro -w /w python:3.12-slim python -m unittest tests.test_learned_model tests.test_training_data tests.test_data_model tests.test_runtime_settings tests.test_live_expert -q` | 53项全部通过 |
| mypy/ruff | 容器内安装mypy/ruff/redis后，`python -m mypy --cache-dir=/tmp/mypy-task32`、`python -m ruff check --no-cache .` | 76文件无类型错误，lint全部通过 |
| 页面 | Playwright CLI执行 `tests/browser/data_model_flows.js`、`workbench_flows.js`、`account_flows.js` | 三级菜单、草稿、全量提交、隐藏字段校验、404、502、320/390px、新导航、账户9种宽度均通过；无未捕获JS错误 |
| 文档渲染 | 标准库md_to_html生成临时HTML，浏览器打开 | 架构2个Mermaid均渲染；禁用CDN后原型1个Mermaid优雅降级且源码可读 |
| 镜像构建 | `./scripts/cpu-limited.sh build analytics-api` | 最终镜像 `ae149953cc1d` |
| 镜像集成 | `docker run --rm --cpus=2 --memory=512m -v /tmp/task32-image-check.py:/check.py:ro ai_football-analytics-api:latest python /check.py` | /health、/settings、/data-model均200；资源修改后新进程立即以1核/256MiB运行，硬盘统计可用 |
| 真实资源限额 | 隔离镜像中读取sched_getaffinity/RLIMIT并分配超额内存 | affinity=[0]，RLIMIT_AS=268435456，RLIMIT_CPU=600秒，300MiB分配被MemoryError阻止 |
| 卫生/冻结 | `git diff --check`；87源码文件SHA256比对 | 无空白错误、源码一致 |

集成时曾发现“资源修改后被正常终止的旧任务导致等待60秒”及“未产生的数据目录被算作错误”两项缺陷，修复后重新冻结、重跑全量/类型/lint/容器回归、重建最终镜像；最终集成通过。

生产容器仍运行 `a62c520c0603`，08:19:56启动，未替用户重启；真实资金与场馆写接口未用于验证。历史数据未删除。页面更新与后端镜像暂时不一致的404将在用户用新镜像启动后消除。

交付：业务提交 `ba5d7f7`；草稿 PR [#44](https://github.com/1420970597/ai_football/pull/44)，基于永久历史 PR #43。最终启动命令 `./scripts/cpu-limited.sh up`，本次未部署。
