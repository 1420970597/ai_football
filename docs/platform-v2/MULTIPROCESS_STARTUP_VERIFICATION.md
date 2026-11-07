# P10-03 真实多进程启动验证

日期：2026-10-27。新增可独立运行的 `tests/test_multiprocess_startup.py`，未修改业务实现。仅临时 Docker PostgreSQL 与假配置，未连接生产、未调用真实模型或提交订单。

## 场景与结果

| 场景 | 实际执行 | 验证结果 |
| --- | --- | --- |
| 受控迁移后并发服务启动 | 真正独立 `python -m app.migrate` 完成后，同时创建两个独立 `python -m uvicorn app.main:app` 进程和一个 `python -m app.worker` 进程 | 迁移进程退出0；3个PID各不相同；两个API真实HTTP `/readyz` 均200且内容ready；工作器独立连接池的查询时间持续推进，证明真实领用循环运行 |
| 日常运行无DDL | 三个服务各用一个独立、非特权、无public CREATE的角色；普通表仅业务DML，迁移登记表仅SELECT | 每个角色实际CREATE及修改schema登记均被数据库拒绝；启动前后表/索引/序列名称与所有者、迁移版本/checksum/applied_at完全一致 |
| 正常进程清理 | 测试终止其创建的全部子进程并等待退出 | 三个运行角色在该临时库的backend连接数最终为0，不遗留子进程池连接 |
| 未迁移拒绝 | 空库同时启动2个真实API与1个工作器，仍使用无CREATE角色 | 三个进程均非零退出且输出明确缺少迁移；public表数为0，没有自动DDL；退出后backend为0 |

工作器库无审核任务，因此不会进入Hermes调用。模型、工具、采集令牌使用 `tests/test_config.py` 已有公开假值，模型与配置端点指向隔离loopback的未监听端口；子进程环境从允许列表重建，不继承生产环境、上游代理、存储或管理员bootstrap配置。

这是“受控迁移完成后真实两API和工作器并发启动”验收。迁移本身的并发互斥仍由已有真实PostgreSQL双连接测试覆盖，本次没有让三个普通进程自行迁移，也没有把双协程写成独立进程。

## 资源与隔离

- 使用已有 `ai-football-settlement-verifier:20261027` 镜像，未重建镜像、未安装依赖。
- PostgreSQL镜像固定为 `postgres:16-alpine@sha256:f4610795587492cd4fb887c2dc3de301b813278bb6f616992061141b5f15ac36`。
- PG内存128MiB、CPU0.25；验证器及全部子进程合计内存384MiB、CPU0.5；上限合计512MiB，不另建高负载编译。
- PG使用 `--network none`，验证器使用 `--network container:<本任务临时PG>`，只共享该隔离网络命名空间loopback；不新增Docker网络、不发布宿主端口。
- 一次运行中采样PG约38.61MiB、验证器及子进程约180.4MiB，合计约219.01MiB；这是瞬时快照，不声称峰值。
- 每例UUID临时库及UUID运行角色；退出只删除本例库/角色、本次容器。仅挂载当前项目只读并复制源码后测试。

初次运行配置了低于PostgreSQL要求的16MiB min_wal_size，临时PG拒绝启动，退出125/1日志保留；根因由临时容器日志确认。改为min32MiB/max64MiB后重跑。没有扩大内存预算、关闭业务检查或改动业务源。

## 精确命令与证据

```sh
sh .runtime/multiprocess-startup-20261027/run.sh > .runtime/multiprocess-startup-20261027/frozen-suite.log 2>&1
```

**实际退出0，2项 / 19.630秒 / OK。** 源码摘要位于 `.runtime/multiprocess-startup-20261027/frozen-source-sha256.txt`，包含新测试与实际API、工作器、迁移入口、数据库实现及018迁移。该任务后续不再修改被测源，主线仍需统一最新全量。

在配置了显式临时PG的验证容器内，可独立执行：

```sh
python -m unittest discover -s tests -p test_multiprocess_startup.py -v
```

临时PG允许的主机为既有测试别名；仅本次无网络共享命名空间运行器使用 `127.0.0.1`，并须同时显式设置 `STARTUP_TEST_ISOLATED_LOOPBACK=1`。测试仍校验管理员角色、postgres控制库及无URL查询覆盖，绝不回落默认业务连接。

P10-03原有迁移锁/版本登记与双连接证据加上本次真实多进程正常/拒绝路径，已经覆盖节点所需隔离启动契约。此证据不等于远端生产冷启动、旧生产数据升级或整个平台最终发布验收。
