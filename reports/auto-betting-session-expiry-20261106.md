# 自动投注无法执行：场馆会话过期响应被HTTP分支吞掉

更新时间：2026-11-06 05:21 UTC。源码提交：5e3f0f29a5dd5384c2103b6cd2f358dc1cb88f01。分支：fix/TASK-78-hermes-telegram-sync。

## 已确认事实与因果链

1. 本轮本人自动投注许可已开启，紧急停止关闭，允许专家2名、限额有效、会员有效、绑定版本与本人许可匹配。API部署开关、后台总开关、远端桥与executor开关全部true。本轮不是11月4日的部署未开放问题。
2. 账户最后成功校验停在2026-11-05 10:50:34 UTC。从10:51:48开始，诊断时累计2997次account_http_rejected / 401；校验时效约18小时，超过资金预检120秒。未生成资金订单，不能把它写成已提交失败。
3. 在实际远端executor容器中，以当前本人会话单次读取余额：HTTP401，JSON业务码0401013，data为空；未进行登录、发码或交易。该码属于项目已有明确会话失效契约AUTH_CODES。原始响应、账户标识、凭据及地址没有进入报告。
4. AccountProtocolClient._request在HTTP>=300时立即抛通用account_http_rejected，尚未读取响应中的0401013。Executor.account_job只捕获AuthExpired执行受租约保护的本人续期。因此后台一直读取旧会话，从未走到恢复路径；本人许可虽然开启，资金预检始终因校验过期而阻断。

## 修复

对响应先执行原有限长读取与JSON解析。只读请求的HTTP401/403同时带既有AUTH_CODES时抛AuthExpired，触发原有个人账户续期。仅状态码、无效JSON、权限拒绝0401015、限流429与5xx均不能授权续期。真实提交路径不改变未知结果策略，HTTP错误不会重发资金请求。

继续使用本人App token、身份、配置版本及远端租约；只有App token明确过期才允许原登录流程。不借用采集账号，不代改个人策略，不补造余额或时间，不补投过期推荐。

## 验证与发布

- 修复前回归：独立worktree执行新增协议场景，exit1，401/403携明确过期码的4个子场景均复现原错误。
- 修复后：Docker协议23项exit0，共用App续期27项exit0，临时独立数据库账户状态47项exit0；合计97项，零跳过。新增链路覆盖旧会话401、本人App token启动、余额恢复、配置版本更新、个人策略完全保留及权限拒绝不续期；既有未知订单核对、单次发送、越权和限额场景通过。
- 首次账户套件仅临时库清理缺少pg_signal_backend而exit1，业务断言通过；补齐本轮测试角色既有标准权限后原47项重跑92.996秒exit0。原失败日志保留，没有屏蔽测试。
- 被测源码冻结在独立worktree；后切到修复提交且干净后Docker构建。构建资源为已有BuildKit CPU0.5/内存384MiB。镜像ai-football-executor:2.0.0-dev-autobet401-5e3f0f2，ID sha256:13e47479eab72ede5528c95edf5dcf7618738b3b597bc5dd62f4ebfdcf6e8bf1，OCI源码标识匹配提交。
- 受保护SSH传输后校验相同镜像ID；无活动资金订单/运行账户任务的短账户事务锁下交接，仅替换远端executor，2026-11-06T05:19:05.552Z启动。保留原Compose覆盖链及live开关；collector、桥容器ID前后完全相同。本机API/web/review-worker未部署。交接锁显式释放。旧镜像和私有previous.json保留供回退；回退不恢复已撤销令牌。
- 新执行器自然账户任务05:19:37开始续期，05:19:59.671557成功。postflight时health=healthy、renewal=ready、配置版本10→12、校验年龄约5秒，资金预检时效恢复。本人策略指纹前后相同，许可仍开启且会员有效。新执行器running、restarts0、错误类别空。

## 验收范围与后续条件

已完成协议与账户状态隔离验证、线上无资金恢复；没有发送测试投注，没有用真实资金验证成交。05:20 UTC所选专家五分钟内新pending推荐0，当前无订单属于无候选，不代表提交协议已真实验收。自动执行仍必须满足新鲜、有效、滚球、本人允许专家、限额及官方当前预检；不追投已过期或已结算推荐。赛前协议未核验仍不开放。

原始私有证据位于.runtime/auto-betting-investigation-20261106（已忽略），包括before-test.log、protocol-test.log、renewal-test.log、account-test-first.log、account-test.log、build.log、image.json、handoff.log、before-policy.json和after-policy.json；均不包含秘密值。进度与命令结果见docs/platform-v2/AUTO_BETTING_PROGRESS.md。本轮未运行全仓套件或浏览器套件，因没有前端/API行为改动；验证范围明确为受影响账户协议、续期和账户/订单状态链路。

持续观察结果（05:21 UTC）：第二个自然刷新在05:21:05.654143再次succeeded，校验年龄约18秒，配置版本稳定12，个人策略指纹仍一致，资金订单0、新鲜待执行推荐0。续期审计明确login_submitted=true，说明既有App token也已失效，原受租约登录流程执行一次并成功，不能将本次真实恢复描述为仅复用App token启动；隔离测试则覆盖了token有效时只启动不登录的路径。
