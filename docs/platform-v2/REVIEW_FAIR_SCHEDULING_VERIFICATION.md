# 审核队列按专家预算与公平调度证据

更新时间：2026-10-26。对应任务 P25-02；隔离验证通过，所属代码已冻结。本记录只涉及现有审核队列，不涉及真实模型、Telegram 登录、乐鱼登录或资金操作。

## 目标与边界

先前审核工作器只在队列头部使用 `SKIP LOCKED`，进程内并发上限不能限制同一专家在多工作器、多个来源中的总占用。高活跃来源的旧任务可能持续排在其他来源之前。

本次保留原队列、任务编号、尝试编号、租约、审核轨迹及现有工具权限；新增的来源与专家表只保存调度水位和并发预算，不保存另一份任务。责任范围为业务审核工作器、编号 012 的增量迁移、审核工作器测试和本证据记录。没有修改身份、账户、前端、主进度或主任务规划。

## 实现与不变量

1. 每个任务的调度来源与专家从其引用的原始消息推导。迁移回填旧任务；插入或改写任务引用时，数据库触发器重新推导，调用方传入的假来源或假专家不会生效。引用缺失时拒绝写入。消息三元唯一键与任务复合外键还阻止已关联消息事后改写来源或专家而产生调度漂移。
2. 专家预算存储在数据库中，默认同时有效租约为 1，允许范围为 1 至 8。同一专家跨群组仍共用一个预算；不同专家可同时占用进程的两个默认工作位。预算下调时让既有有效任务自然排空，不驱逐有效租约。
3. 先按来源上次访问水位，再按专家水位选择有可领取任务的可信组合。访问组合后立即推进持久水位，包括组合头部被其他事务锁定的情况。高积压来源不能仅因消息较旧而一直优先。
4. 单次领取最多探测 8 个来源与专家组合。一次未取得任务仍提交访问水位；下一次调用继续轮转，不从同一个锁定组合反复开始。公平性指符合资格且存在可用预算、已到可执行时间的组合轮转，不承诺上游或模型故障时的固定完成秒数。
5. 组合内优先接管已到期的未耗尽任务，再取已到执行时间的待处理或重试任务；使用原来的行锁及 `SKIP LOCKED`。同一任务接管时产生新尝试编号，旧尝试不能续期或写入。
6. 所有领取与心跳续期使用同一个短事务咨询锁，防止检查预算后，已到期的旧尝试被并发续活而超额。该锁不覆盖模型运行期。咨询锁立即尝试，行锁等待最多 2 秒、单条语句最多 3 秒，领取整体最多 5 秒。领取遇到锁、语句或整体截止时记录暂缓类别，回滚该次调度，后续循环再试，不因短暂调度竞争退出整个工作器、取消其余有效模型调用；连接等其他错误仍正常上报。耗尽任务的失败转换每次最多 100 项，并跳过被锁定任务。
7. 心跳锁繁忙或单次数据库等待超时返回“暂未续期”，不冒充所有权失效；在原有效租约截止前短退避重试。单次续期整体最多 5 秒且不超过剩余租约预算。尝试编号改变、来源停用、消息不再有效或租约真实到期时才终止相应审核。
8. 时间范围判断使用每条语句稳定的数据库时间，保留时间范围索引使用条件；真正延长租约时使用实际数据库时钟。来源、专家、可信关系、媒体就绪、删除状态和最新消息版本仍参与领取与工具写入校验。
9. 工作器退出保留已有租约，取消本地模型任务并清理 Hermes 会话。其他工作器等租约到期后接管，避免退出瞬间释放预算而让尚未终止的调用与新调用重叠。正常租约为 90 秒、心跳间隔 20 秒。

## 结构、恢复与部署约束

调度键具有到来源和专家的外键，并通过复合外键保持与原消息一致；消息仍是身份事实。消息三元唯一索引用于引用约束，其余新增索引分别服务组合待处理任务、组合运行任务、专家有效租约及水位顺序。领取只枚举来源与专家元数据，再对有索引的任务组合探测；没有每次对全部消息排序的队列副本。极大专家或可信映射规模仍需要另测查询计划，当前测试不代表百万来源性能。

新来源与专家首次调度时补建元数据，不要求每个旧调用方同步修改插入字段。备份恢复按外键顺序先恢复来源、专家、消息，再恢复任务；二进制 COPY 仍执行任务推导触发器。元数据没有需另存状态的序列或必须豁免空库校验的单例种子。

编号 012 对旧任务回填并设置非空约束，部署到真实持久库前仍需备份及迁移耗时评估。本次只验证临时数据库。上线时全部工作器必须使用同一新版本；旧版本工作器不会理解数据库预算，不能混跑后声称预算有效。预算当前是数据库运维策略，未增加对外管理设置入口。

## 验证场景

审核工作器共 25 项测试，含原有 7 项与新增 18 项：

- 两位专家分别高积压时均能取得默认并发工作位；同一来源内专家轮转，以及 40 条旧积压来源与新来源轮转。
- 两个独立连接池并发领取，同一专家跨两个来源始终只持有一个有效租约；数据库预算 2 及降至 1 的排空行为。
- 正常心跳与并发领取不超额、不替换持有人；咨询锁短暂繁忙不会误取消；持续繁忙到真实截止后终止；单次续期整体超时取消其等待。
- 锁定队列头部跳过下一项；9 个锁定组合使第一次探测恰好用完预算，第二次继续轮转并取得未锁定组合；耗尽任务被锁定不会阻塞其他专家。
- 外部事务锁定来源记录时，领取在规定时间内暂缓且留有类别日志；释放来源行锁后同一任务仍可领取，不把竞争冒泡为整个工作器退出。
- 到期尝试优先恢复、旧尝试不能复活、最终尝试进入失败状态；停止来源取消模型替身；进程取消保留租约，之后到期恢复。
- 从迁移 011 的旧结构升级、原有插入字段继续可用、调用方伪造调度键被纠正、已关联消息不能改写来源及专家、外键恢复顺序及二进制 COPY 恢复。

另外回归原审核工具、管理员重试和备份恢复。测试输入均为合成消息和模型替身；没有访问真实上游。

## 执行记录

初轮 20 项运行中 6 项错误源于测试辅助函数与既有专家编号属性同名，已更名修复；未改业务断言。一次 20 项重跑的工具会话输出未保留，不作为通过证据。增加锁定组合、真实租约截止、总截止及锁定死信用例后，24 项定向回归为 **15.159 秒，退出码 0**。

随后在容器内复制源码成冻结副本运行 64 项回归，**36.965 秒，退出码 1，63 通过、1 个清理错误**。错误为共享测试库恢复测试 `DROP DATABASE ... WITH (FORCE)` 缺少终止后台进程权限，发生在清理阶段；资金及引用恢复断言已通过。未改断言掩盖错误，也未修改共享数据库权限或停止其他人的容器。共享测试库没有设置资源上限，因此最终验收改为本任务独占且受限的 PostgreSQL 实例重跑。

主体独占隔离回归 **64 项全部通过，116.259 秒，退出码 0，无失败、错误或跳过**。其中审核工作器 24 项、原审核工具 11 项、管理员审核 17 项、恢复 12 项。恢复包含 41 张表、1 个媒体对象；恢复前后 2 用户、1 消息、2 信号、2 订单、本金 4.00、预留敞口 2.00、一单位净收益 0.950000、合成成交净收益 1.90000，差异为 `{}`。备份 0.503623 秒、恢复 0.689511 秒只对应这个合成小样本，不代表生产耗时。

随后补入消息复合外键并增加事后身份改写拒绝断言，**24 项工作器与 12 项恢复共 36 项，71.734 秒，退出码 0**；恢复仍为 41 表及相同金额、引用、媒体，差异 `{}`，备份 0.334221 秒、恢复 0.886122 秒。最后补入领取行锁超时暂缓处理与新增用例，冻结副本复跑 **25 项工作器，47.902 秒，退出码 0，无失败、错误或跳过**；没有改动已通过的恢复实现。最终运行前后以下三个对象摘要一致，临时任务数据库剩余 0，再次清理本任务专属 PostgreSQL 容器与网络。

最终测试环境：测试驱动 0.45 CPU、512 MiB；PostgreSQL 16.15，0.25 CPU、384 MiB、256 MiB 内存盘，总 CPU 上限 0.70。独立内部 Docker 网络不连接外部业务；测试库逐例创建与删除，既有共享库不参与最终运行。结束时查询确认没有剩余任务数据库，随后删除本任务专属 PostgreSQL 容器和网络。冻结对象运行前后摘要一致；差异空白检查退出码 0。

执行命令（数据库口令仅是该临时合成测试环境的公开固定值）：

```sh
docker network create --internal ai-football-review-fair-net
docker run -d --name ai-football-review-fair-pg --cpus=0.25 --memory=384m \
  --network ai-football-review-fair-net --network-alias ai-football-v2-pgtest \
  --tmpfs /var/lib/postgresql/data:rw,size=268435456 \
  -e POSTGRES_USER=recovery_test -e POSTGRES_PASSWORD=isolated-recovery-test-only \
  -e POSTGRES_DB=postgres --health-cmd 'pg_isready -U recovery_test -d postgres' \
  --health-interval=1s --health-timeout=2s --health-retries=20 \
  sha256:f4610795587492cd4fb887c2dc3de301b813278bb6f616992061141b5f15ac36
docker inspect --format '{{.State.Health.Status}}' ai-football-review-fair-pg
docker run --rm --name ai-football-review-fair-regression --cpus=0.45 --memory=512m \
  --network ai-football-review-fair-net -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/tmp/fair/services/api:/tmp/fair:/tmp/fair/tests \
  -e TEST_DATABASE_ADMIN_URL=postgresql://recovery_test:isolated-recovery-test-only@ai-football-v2-pgtest/postgres \
  -e RECOVERY_TEST_ADMIN_URL=postgresql://recovery_test:isolated-recovery-test-only@ai-football-v2-pgtest/postgres \
  -v /root/ai_football:/source:ro -w /tmp ai-football-verifier:v2.0.0-dev sh -eu -c '
mkdir -p /tmp/fair/services /tmp/fair/infra /tmp/fair/tests
cp -a /source/services/api /tmp/fair/services/
cp -a /source/infra/recovery /tmp/fair/infra/
cp /source/tests/test_review_worker.py /source/tests/test_reviews.py /source/tests/test_admin_reviews.py /source/tests/test_recovery.py /tmp/fair/tests/
cd /tmp/fair
python - <<"PY"
import logging
import unittest
logging.getLogger("httpx").setLevel(logging.WARNING)
suite = unittest.defaultTestLoader.loadTestsFromNames(["test_review_worker", "test_reviews", "test_admin_reviews", "test_recovery"])
result = unittest.TextTestRunner(verbosity=1).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
PY
'
docker exec ai-football-review-fair-pg psql -U recovery_test -d postgres -tA \
  -c "SELECT current_setting('server_version'),count(*) FILTER (WHERE datname NOT IN('postgres','template0','template1')) FROM pg_database"
docker rm -f ai-football-review-fair-pg
docker network rm ai-football-review-fair-net
```

验证器镜像本地内容摘要为 `sha256:000b6661b884badd7cf5f1b940b9ee89d76c311ba091afaea8e1eb7a0c42cd01`；PostgreSQL 镜像为上述本地内容摘要。这里只记录实测本地镜像，未声称对应正式发布资产。

两次必要复验沿用上述受限环境与冻结复制过程。36 项只加载 `test_review_worker` 和 `test_recovery`；最终 25 项复制审核工作器测试及其审核工具测试支持，然后在 `/tmp/fair` 执行 `python -m unittest test_review_worker -q`。每次均保留原测试退出码，没有使用管道或后续成功命令覆盖失败。

冻结对象 SHA-256：

| 对象 | 摘要 |
| --- | --- |
| 审核工作器 | `77a44d7eaa36f780d21cd8366ee00e2d872660521c65ec8f55ccd803cc86b416` |
| 迁移 012 | `224a48d6198e3d6a8eef4f9319f6baea0860b24fcee7ec4605c71c73093ab244` |
| 审核工作器测试 | `778bc3844106b1017675457559961c786ed7bcd336b8d4f2e95e861c66d44058` |

## 尚未验证

真实远端数据库的迁移时长、网络抖动、模型提供方的实际并发取消、生产规模吞吐及长时间重启演练仍缺证据。数据库租约和工具权限保证的是接受与写入围栏，不保证第三方模型服务收到断开后立即停止计费。本阶段不能据此宣布真实业务恢复或生产发布。
