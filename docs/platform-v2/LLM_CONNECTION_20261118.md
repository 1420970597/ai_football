# 管理员 LLM 连接热更新

更新时间为 2026-11-18 09:29 UTC。当前目标是在管理中心配置 Hermes 的模型网关连接，并在保存成功后应用于新启动的审核。

| 工作包 | 责任目录 | 前置依赖与证据 | 设计与实现 | 验收与失败处理 |
| --- | --- | --- | --- | --- |
| 管理连接与运行时切换 | `web/app/`、`services/api/`、`integrations/hermes/` | 现有管理员会话与 CSRF、Hermes 固定镜像的逐请求 agent 路由、加密配置及审计表。原连接从 Hermes 环境读取 | 入口校验管理员权限，密钥请求体直接交 Hermes。Hermes 验证连接，将加密配置及版本交业务数据库持久化，再更新运行时值。显式审核模型优先于连接默认模型 | 验证桌面与手机、越权、密钥留空与清除、版本冲突、失败维持当前连接、无需重启的模型请求与重启恢复。发生写入不确定时重新读取持久状态 |

状态为设计已记录。`git status --short --branch`、`git ls-remote --heads origin fix/TASK-78-hermes-telegram-sync` 和对运行中 Hermes 镜像路由的只读核对均退出 0。分支起点为 `c45f6291`。

08:52 UTC，状态为代码已写。`git diff --check` 退出 0。开始冻结源码并运行 `bash tests/integration/run_hermes_connection.sh`，下一步核验浏览器经过 Nginx 到 Hermes 的请求、模型切换和重启恢复。

09:19 UTC，首轮集成命令退出 1。1440 px 通过。390 px 清除密钥已阻止上游调用，框架将运行时错误包装为 HTTP 200，失败断言定位到正常的审核入口。已加显式 503 和审核入口认证，按原命令重跑。

09:21 UTC，`bash tests/integration/run_hermes_connection.sh`、`bash infra/check-runtime.sh`、`bash tests/browser/model-configuration-check.sh` 均退出 0。已验证两个视口、真实容器转发、版本冲突、失败保留旧连接、密钥清除和重启恢复。复核发现当前运行的 Web 有版本化资产，按运行中镜像上的实际文件制作局部候选。下一步在最终冻结后验证并准备切换。

连接管理需要独立的 `HERMES_CONNECTION_TOKEN`（API 与 Hermes）和 `HERMES_CONNECTION_STORAGE_KEY`（仅 Hermes），均为外部受保护配置中的独立 32 字节十六进制值。启用时沿用现有模型网关作为初始版本。持久密文存于业务数据库，恢复时须同时保有 Hermes 加密密钥。地址和默认模型经一次文本模型调用验证后切换，清除密钥会暂停新审核的模型调用，已开始执行的 Agent 继续使用其启动时的连接。

09:25 UTC，最终冻结集成命令退出 0，另外 22 项已有回归退出 0。状态为隔离验证通过。生产基线补丁首次套用退出 1，网页的 JS 和 Nginx 存在不同上下文。按现有版本化入口插入已验证的功能段后，两次 `docker build` 均退出 0。部署配置差异检查识别到基础 Compose 新增的空凭据项和管理网络，需要从回滚基线投影排除，下一步完成复核、切换和只读验收。

09:29 UTC，功能提交 `4eb12d01bd79cb49eaec6298d3063e6f31baf7ac` 已推送至原任务分支，`git push` 与 `git ls-remote` 均退出 0。原容器环境、网络与挂载核对通过，候选及回滚的 `docker compose config --quiet` 退出 0。切换前活动审核租约为 0，现有连接覆盖记录为 0。

API、Hermes、Web 依次执行 `docker compose up -d --no-deps --no-build --pull never`，三个服务均通过健康检查。随后合并检查命令因审核工作器没有 Docker Health 字段而退出 1，改为条件读取后 `docker inspect` 退出 0，确认三个服务健康、审核工作器仍使用原容器。API 和 Web 镜像分别为 `sha256:ccc552f3ff32ae3c9eb463e0a5989a0c34b6578eeafd471223b2559bed3003a1` 和 `sha256:fdea47c4b9b12273903b26f3a36f4a709810bd79d815e8eaa225db56d9472756`。Hermes 沿用固定版本镜像和生产技能挂载，加载新的连接管理扩展。

生产 Web 原入口加载新版管理脚本及版本化二维码资源，容器中的 `wget` 核对退出 0。生产管理员通过 Web 读取版本 0，确认密钥状态为已配置、响应不含密钥且禁止缓存。匿名请求返回 401，内部存储路由经 Web 返回 404。容器中的端到端只读核对退出 0。线上仍使用原模型连接，真实配置写入及模型切换验证仅在临时库和隔离网关中完成。状态为该功能已部署且真实只读联调通过。

本机继续启动时沿用受保护的 `.runtime/hermes-connection-release-20261118/candidate.private.json`。同目录的 `rollback.private.json` 记录原有镜像与挂载，`recovery-keys.env` 保留解密及服务认证所需密钥。回滚以原配置依次重建相同三个服务，持久密文保留在数据库。后续部署需保留受保护的密钥和版本化 Web 资源。
