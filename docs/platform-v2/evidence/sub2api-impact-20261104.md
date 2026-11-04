# 8885 / sub2api 影响核查

- 更新时间：2026-11-04 18:51 UTC（核查结束）。
- 当前目标：回答球讯项目是否影响本机 8885 上的 sub2api；仅诊断，不重启、部署或修改其他项目。
- 活动工作包：端口、容器隔离、共享资源和近期容器生命周期核查；只读采样已完成，历史故障因果未验收。
- 责任目录：`docs/platform-v2/evidence/`；前置依赖为 Docker、系统日志与现有运行容器的只读可见性。
- 已有证据：8885 映射 sub2api:8080，球讯公开网页为 3003；sub2api 三容器 healthy；2026-11-04 18:46:33–34 UTC 三容器新建，应用 18:46:43 UTC 启动，当前 restart=0 / OOMKilled=false。网络与持久目录和球讯所检查 API/Hermes 容器分离。
- 资源快照：4 CPU / 7.8 GiB RAM，可用约 3.1 GiB；swap 已使用约 3.3 GiB，根盘 86%；球讯后台测试和模型服务正在运行，有共享 CPU、内存、磁盘竞争可能，快照不足以证明历史故障因果。
- 实际命令与退出码：`git status --short --branch`、`docker ps -a --format ...`、`ss -ltnp '( sport = :8885 )'`、定向 `docker inspect --format ...`、`docker stats --no-stream --format ...`、`uptime` / `free -h` / `df -h` / `vmstat 1 3` 均 exit 0。`journalctl -k --since '6 hours ago' --no-pager --grep='oom|Out of memory|Killed process' -n 30` exit 1（无条目，并非证明所有历史均无 OOM）；Docker daemon 近一小时日志查询 exit 0，记录 18:46:30 拉取 sub2api 镜像及随后启动。按 create/start/die/kill/stop/destroy/oom 过滤的近期 Docker events exit 0 但无输出，事件缓存不能充当完整历史。
- 缺口：重建前的状态、发起重建的操作者/原因、用户实际异常时段、模型网关是否关联 8885。
- 设计决策：只发送少量无认证健康/首页 GET；探针在受限 Docker 容器内运行，不执行模型生成或真实交易，不输出配置秘密。
- 下一步与验收：核对实际模型地址仅输出目标主机/端口；检查宿主映射端口的 HTTP 响应；保留无法归因的历史缺口。探针失败保留错误类别，不触发重启或修改其他服务。

## 核查结论与完成证据

- 存在直接依赖：Hermes 运行环境中的 `HERMES_PROVIDER_BASE_URL` 指向本机公网地址的 8885，`ip -4 -brief address` 与目标地址一致。因此模型请求会使用 sub2api 的处理能力及相关上游配额，不能声称两项目完全无关；这也不等于已证明 sub2api 因球讯而故障。
- 未发现端口占用冲突；当前 sub2api、PostgreSQL、Redis 均 healthy、restart=0、OOMKilled=false。这些状态只覆盖当前容器，不代表旧容器或之前时段没有故障。
- 受限临时探针 `docker run --rm --name ai-football-sub2api-readonly-probe-20261104 --network host --cpus 0.1 --memory 64m --memory-swap 64m --pids-limit 24 --read-only --cap-drop ALL --entrypoint python sha256:26736a5a29c7e8e514551875b40807ab6a7c9a4e2e025440051a0f024343dac1 -c '<stdlib urllib GET /health, /, /health; only print status/type/time>'` exit 0；三个响应均 200，耗时分别 191.80 / 2.67 / 0.91 ms；未发模型生成请求，容器自动移除。
- 原地址读取脚本因 `import yaml` 不可用 exit 1，未安装依赖；改用标准库 `os` / `urllib.parse` / `re` 只读目标主机/端口后 exit 0，未输出凭据。`rg` 两次包含猜测不存在路径 exit 2，仅作为发现路径失败保留；实际网关结论来自运行环境和已存在的 compose 定义，不依赖这些失败搜索。探查可选 Python 路径时 `ls` 因部分不存在路径 exit 2，未修改容器。
- `docker logs --since 15m --tail 2000 sub2api 2>&1 | docker exec -i ai-football-v2-hermes-1 python -c '<stdin aggregate; no raw logs>'`（`set -o pipefail`）exit 0，仅34行当前容器日志；有1行匹配 timeout/deadline 文本，无法仅凭关键词确定故障或归因。日志内部时间无显式时区，不与 UTC 生命周期直接混用。
- 18:46:30 UTC Docker daemon 记录拉取 sub2api 镜像，18:46:33–43 UTC 当前三容器创建启动；未找到是谁、为何发起此操作的证据，因此不能把“当前容器创建”直接判定为崩溃重启或球讯操作。
- 保留局限：本机有资源竞争及 swap 使用，短采样不足以归因用户此前异常；未验证认证模型生成和上游实际可用性，也未对旧容器作无证据推断。
- 下一步：如需追溯具体故障，按用户实际异常时间比对重建前日志、模型请求统计及部署操作记录。本轮未改变任一运行服务、配置、数据库或限额，仅新增本诊断记录。
