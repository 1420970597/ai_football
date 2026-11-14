# 个人乐鱼账户校验失败诊断

更新时间：2026-11-04 19:12 UTC。范围：展示名称为“222”的唯一启用账户；只读诊断，不提交登录、取新代理、修改配置或投注。

## 结论与真实证据

用户截图混合了历史任务与当时尚未结束的任务。19:08:19及19:09:09 UTC的远端数据库只读查询确认，最新任务已经失败，没有 queued/running 任务，不能将截图中的“校验中”解释为现在仍在运行。

| 任务创建时间（UTC） | 结束时间（UTC） | 绑定版本 | 最终状态 | 拒绝环节与证据 |
| --- | --- | --- | --- | --- |
| 2026-10-30 04:25:47 | 04:26:13 | 1 | rejected / app_login_rejected | 续期审计 code=6026，login_submitted=true |
| 2026-11-04 19:04:57 | 19:05:30 | 2 | rejected / app_login_rejected | 续期审计 code=6026，login_submitted=true |
| 2026-11-04 19:06:08 | 19:06:36 | 4 | failed / proxy_login_proxy_http_rejected | 续期审计 code=410，login_submitted=false |

前两次是乐鱼 App 登录接口 `/site/api/v1/user/login` 返回业务码6026；该接口成功码为6000。当前代码未保留上游解释文本，仓库授权协议资料未找到6026的确定映射，因此不能断言密码错误、账号冻结、签名错误或风控中的某一种。

最新一次是 HTTP 代理 CONNECT 阶段拒绝，工作器将 httpx.ProxyError 中的数字状态保留为410。`_submit_login` 捕获 `_RequestNotSent` 后清除已发送标识，续期审计因此为 login_submitted=false；本次不是乐鱼账户密码校验结果。410背后的提供方原因（代理租约、目标策略、认证或资源状态）没有在当前脱敏审计中记录，不能只凭410确定。

当前账户为 health_state=unverified、renewal_state=ready、config_revision=4、binding_revision=4，个人会话不存在，last_verified_at和balance_observed_at均为空，未保存已核验余额。本人用户名、密码、设备标识均存在且可解密；登录代理配置存在。自动投注关闭、紧急停止为true、无有效账户投注许可。没有输出凭据或余额值。

协议设置时间：App主机在2026-11-04 16:48:26 UTC更新；签名、发现、登录代理配置均最后更新于2026-10-26 16:15:59 UTC。最新个人失败晚于主机更新，不能把旧报告中的“更新后尚未复核”当作本次事实；没有证据表明当前问题仍是旧私有主机覆盖。`private_config` 已排除私有会话主机覆盖，也不会将全局采集令牌给个人账户。

## 页面为何呈现这些文字

- `web/app/app.js:1073` 按账户health_state显示“未校验”；普通failed回执在`services/api/app/routes/accounts.py:412`不会更新health_state，所以最新代理失败后标题仍是“未校验”。
- 最近校验及余额时间取成功校验字段；首次校验一直未成功时均为空。显示“时间未知 / 尚未核验”不能解释为零余额。
- `get_leyu_account`按用户取最近五条任务，不过滤绑定版本；页面逐条渲染但不显示任务时间和绑定版本，因此两个不同日期、不同绑定版本的历史拒绝看起来重复。
- 提交任务后仅调用一次`loadAccount`，账户页没有持续轮询；底部30秒定时器仅刷新比分页。因此截图可停留在running，即使任务28秒内已结束。已部署`/usr/share/nginx/html/assets/app.js`与工作树文件SHA256均为`1f7de2b32d6b49a27fead3a1f185d91c4d2bc8431842caabda7a7a57083a18c2`，该发现适用于当前部署源码；本轮没有另做浏览器交互验收。

## 后续处理顺序

1. 先核查现有登录代理提供方对CONNECT 410的解释、授权与短期租约配置，保持代理只用于已授权登录，不通过更换身份或盲目反复登录排障。
2. 对6026，先由账户本人确认官方App登录结果，或依据已授权的上游业务码说明/原始脱敏错误文本继续定位。当前采集成功不能证明个人账户凭据正确。
3. 页面分别呈现当前任务及带时间的历史记录，在排队/运行期间刷新，完成后停止；显式显示代理故障与已核验余额缺失原因。保留上游数字码用于诊断，不向用户输出原始响应或凭据。本轮仅分析，未实现或部署这些改动。

## 实际检查与失败保留

所有Python与数据库操作均在现有API Docker容器内执行，单独连接、timeout=8秒、command_timeout=8秒、statement_timeout=8000、lock_timeout=1000，`default_transaction_read_only=on`且事务`readonly=True`，两次`SHOW transaction_read_only`均为on。仅一条账户、最多八条任务和二十四条账户审计；未全表导出日志。

- `git status --short --branch`、`git log -5 --oneline`、`rg`定向源码检查、`docker ps --format ...`：exit0。
- `docker exec -i ai-football-v2-api-1 python - <<'PY' ... PY`首轮：exit1。账户、任务、审计读取已返回；末尾设置查询误用了不存在的`v2_admin_settings.version`。查明实际结构后改为`SELECT setting_key,updated_at ...`，同容器只读重跑exit0；没有修改数据库。
- 重跑同时调用纯读取的`account_jobs.private_config`与`reverification_available`：只打印配置存在性布尔值、非敏感状态与时间。个人令牌/场馆会话均缺失，登录代理存在；reverification_available=false仅表示不适用“协议更新后的拒绝复核”，不是新任务仍在运行。
- 初次猜测Web路径`/usr/share/nginx/html/app.js`、`/usr/share/nginx/html/app/app.js`的ls：exit1。后续源码查找使用错误Dockerfile路径的rg：exit2。查阅实际`web/app/Dockerfile`后，对正确`assets/app.js`重跑sha256sum，exit0，与工作树相同。
- 本轮未重放登录、未主动调用代理提供方、未发模型请求、未改业务记录或运行配置；无应用代码变更，未执行应用测试。审计证据证明工作器此前执行并回传结果，本轮未额外SSH核查远端进程存活。
