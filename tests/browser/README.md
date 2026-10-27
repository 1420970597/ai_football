# 网页隔离验收

在仓库根运行 `sh tests/browser/check.sh`。只使用本项目挂载与本任务临时 Docker 网络，0.75 CPU / 1GiB / 256进程；没有远端生产依赖。测试服务器运行在同一容器，所有 API 由浏览器固定样本拦截，网络为无外网路由的内部网络，便于真实模拟 navigator.onLine。完全禁网容器会一直报告离线，不适合正常网页加载场景。

`results.json` 是最近一次真实执行的逐场景结果。截图输出到参考研究目录，名称含 `fixture` 均为隔离固定样本，不能作为真实数据、盈利或上线证据。

官方参考采样单独执行：`docker run --rm --cpus .5 --memory 768m --pids-limit 256 -v "$PWD:/work" ai-football-browser:v2.0.0-dev tests/browser/reference.mjs`。只访问公开官方网页，不登录原站。该命令需要网络，不能与禁网的业务行为测试混淆。

固定 Playwright 1.56.1、完整 Node 依赖锁、基础镜像摘要；二维码生成器版本1.4.4独立保留MIT署名。测试覆盖真实浏览器事件、布局、双会话CSRF、文图与多推荐、授权错误、重试、分页、深链、筛选与风险开关，不调用实际资金接口。

真实API隔离联调另运行 `sh tests/browser/integration-check.sh`。该脚本先复制冻结源码，再重建API和Nginx镜像，分两轮建立任务专属内部网络和tmpfs PostgreSQL。第一轮注入4场人工赛事、4条报价、2条消息和2条推荐，覆盖原17场景、5身份82项权限断言；注册、管理员MFA、Cookie、CSRF、会员授予及SQL查询均通过真实API完成，没有HTTP mock，实盘总开关关闭。第二轮使用独立空库与明确的账户替身，开启隔离测试资金开关，点击来源及信任管理、会员授予撤销、本人账户核验、专家和金额限额、启停、近期身份重验及订单核对；不启动真实采集、模型或订单执行器，网络无外部路由，域名和凭据全部是无效测试值。

两轮输出分别为 `integration-results.json` 与 `integration-account-results.json`，服务日志和冻结源码摘要留在本地忽略的证据目录；脚本trap删除自身容器、网络和临时数据库，并将本轮结果与截图回填持续集成输出路径。第二轮必须核对替身核验、刷新和回执计数以及交易调用为零。媒体S3未配置时真实授权端点返回503，单独列出而不声称真实媒体已验证。浏览器结果中的“真实API”只指当前源码接口和临时数据库，不能解释成真实上游/生产验收。

报告离线验收为 `report-check.mjs compile` → Docker内生成HTML → `report-check.mjs`。同一固定浏览器镜像额外安装锁定的Mermaid11.4.1，只用于构建本地流程SVG，不进入生产网页。无参数检查在network none下以本地HTTP加载报告，验证每张图片解码、流程图及可展开源码、1440/390布局与外部请求为零；结果保存到 `report-results.json`。
