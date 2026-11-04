# E243 固定转发的独立 SSH 候选

状态：隔离定向验证通过，未部署。2026-11-04 20:59 UTC。

目标是在现有 remote-link 容器和预算内隔离 API 上行与控制回程，不新增业务目标、访问权限、凭据或并发业务重试。责任目录为 infra/remote、tests；依赖 E240 对照及 E239 请求阶段证据。源为 14508b3，排除并发会话的账户变更。

E240 的 31 轮交错健康探测中，原连接 P50/P95/max 为 522/1425/6619ms，临时独立连接为 243/334/401ms，均 200。该短样本存在连接年龄及负载混杂，也未重现十秒失败，因此不能据此宣称已查明或消除全部根因。E239 完整观察中仍有 API 数据库超时，传输拆分不会自动修复它们。

配置仅增加严格布尔 `split_connections`，缺省或 false 保留原单连接；true 为两个已存在固定目标分别启动 SSH 子进程，显式禁用 ControlMaster/ControlPath 复用。任一子进程退出时回收其他子进程；第二次启动失败也回收第一个。状态以 0600 临时文件原子替换，健康检查验证全部 PID 后才检查带认证的控制链路，同时兼容旧单 PID 状态。没有扩展远端 shell 或目标选择能力。

隔离验证命令：Docker 受限 runner，运行 `python -m unittest tests.test_remote_deployment tests.test_remote_link_lifecycle`；独立临时 PostgreSQL，runner .5CPU/512MiB、PG .25CPU/256MiB。首次 30 项 11.770 秒 exit1，发现 Path.open 不支持 opener，保留 e243-target-01.log；改为内建 open 后重跑 31 项 13.590 秒 exit0。包含真实子进程任一退出、部分启动失败、SIGTERM 回收、私有原子状态、无效及死亡第二 PID 拒绝、原 HTTP/Unix 传输契约。两次临时容器和网络清理。

下一步：冻结提交后完整 Docker 后端套件。当前生产 E241 观察到 21:23:46 以后方可终审；期间不修改已挂载生产脚本。后续若部署，只切换本项目 remote-link，核验原项目 socket、权限、预算及其他容器，开启另一完整 30 分钟实测；故障按记录恢复旧脚本及配置。稳定性、3 秒目标、网关上游权限拒绝均未验收通过。

2026-11-04 21:41 UTC更新：冻结8ebbd87完整1291项632.752秒exit0、零失败错误跳过，恢复difference={}，证据1110970/backend-tests.json SHA46385fd6b0d7d62ce7b430d504b24963befdf59e67907fe16bfe891e9683890d。代码原样cherry-pick为3fe44df；原监督进程在此前保持单连接，21:40:10.134488362 UTC仅项目remote-link重建启用split_connections。部署脚本exit0，健康且两个独立SSH子进程存活；其余六个本机服务ID、启动时间、资源逐项不变。旧配置已受保护备份，回退只还原本项配置并重建本服务。切换前相同31次健康探测全200，P50/P95/max487/600/681ms；切换后对照仍在执行，尚不称长尾或数据库故障解决。
