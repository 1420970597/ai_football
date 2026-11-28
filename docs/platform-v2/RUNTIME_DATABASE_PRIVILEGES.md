# 运行数据库角色最小权限：隔离验收证据

更新时间：2026-10-27。工具隔离验收完成后，主工作线已按既有授权执行远端运行角色创建及API、审核工作器切换；下文分开记录隔离证据与实际切换。

## 已实现的权限边界

- 新建独立、非所有者运行角色，不降权、改密或删除旧迁移身份；不改变其他用户、PUBLIC、旧表、对象所有者及默认授权。
- 运行角色仅有当前库 CONNECT、public USAGE、当前 v2 业务表四类 DML；迁移版本表仅 SELECT。所有全局管理属性、角色成员关系、持久 schema CREATE、旧表访问、TRUNCATE、REFERENCES、TRIGGER 与所有权均拒绝。
- PUBLIC TEMP 权限保留并显式报告；此项不等于持久 DDL。PUBLIC 的持久 CREATE 或旧表访问无法单独对新角色抵消，发现即拒绝，不做全库撤权。
- 应用在单一事务和专用 advisory 锁内执行；新增 v2 表后显式 refresh，再 verify，不为未来旧表设置广泛默认授权。
- 随机密码只写入项目私有运行目录，目录 0700、文件 0600、排他创建且拒绝符号链接、外部路径和覆盖；输出不包含 SQL/连接异常原文或凭据。生成 DSN 保留原 TLS/连接参数，拒绝查询参数覆盖身份。

## 冻结验证

测试使用独立 internal Docker 网络 `ai-football-runtime-role-test` 和 `ai-football-runtime-role-pg`，PostgreSQL 16 固定摘要、数据目录 tmpfs、0.5 CPU/384 MiB。测试连接只接受任务专用数据库别名和 postgres 管理库，每例创建随机临时库并应用当前全部增量迁移，结束后清理临时库及角色。

验证容器为 `ai-football-verifier:v2.0.0-dev`，镜像 ID `sha256:c66bd5b5b62b3571ce8e3f121ecce209bc358de7b8e79a4e6da17f6c79fb0265`，0.5 CPU/384 MiB。容器只读挂载本项目，先复制排除运行目录、环境文件、Git、依赖缓存及构建产物的项目快照，再在快照中加载测试；运行过程中未修改被测代码。

执行命令的容器入口为：

```sh
docker run --rm -i --user 0 --network ai-football-runtime-role-test \
  --cpus=.5 --memory=384m \
  -e TEST_DATABASE_ADMIN_URL=postgresql://postgres@ai-football-v2-pgverify/postgres \
  -v /root/ai_football:/source:ro \
  -v /root/ai_football/.runtime/db-role-evidence:/evidence \
  ai-football-verifier:v2.0.0-dev python -
```

标准输入中的快照运行器加载 `tests.test_runtime_db_role`、`tests.test_config_reuse`、`tests.test_config_runtime`。最终实际结果为 **32 项，失败 0、错误 0、跳过 0，退出码 0**；unittest 用时 8.378 秒，含快照及报告的运行器用时 8.897 秒。机器记录为项目私有目录中的 `db-role-evidence/result-final-3.json`。

覆盖行为包括：

- 新角色业务增删改查和只读结构检查成功；持久建表、建 schema、ALTER/DROP/TRUNCATE、CREATE DATABASE/ROLE、旧表全部读写及迁移版本写入确实返回权限错误。
- 旧迁移身份属性不变，旧表内容保留，PUBLIC TEMP 不变；已有高权限身份不被工具接管或降权。
- PUBLIC schema CREATE、旧表 SELECT、迁移表 UPDATE、已有角色成员关系与对象所有权均拒绝；新增 v2 表只经 refresh 授权，新增旧表仍不可读。
- 直接赋予旧表单列 SELECT 或其他持久 schema CREATE 后 verify 必须失败，不能只检查表级 ACL 或 public schema。
- 无效密码导致事务回滚；外部路径、符号链接、宽权限文件及输出覆盖拒绝；传输参数保留，身份参数覆盖拒绝。

另在同一个隔离 PostgreSQL 创建独立 CLI 验收库，顺序调用 `plan → apply → verify → refresh`，各返回码均为 **0**；捕获输出并断言不含生成密码。使用私有输出中的新 DSN 真正建立连接并执行只读结构校验，管理连接断言密码存储为 SCRAM-SHA-256，全部通过。CLI 专用临时凭据只用于此临时库，验收后删除。

早期失败保留：首次 PG 尚未就绪导致 ConnectionRefused，等待 pg_isready 后恢复；后续私有目录测试发现中间目录创建权限过宽，改为逐层 0700 创建后重跑通过。最后新增旧表列权限与其他 schema CREATE 断言后完成上述 32 项冻结回归，没有用后续成功命令掩盖前次失败。

冻结摘要：

| 范围 | SHA-256 |
| --- | --- |
| 受控配置工具 | `822387a92075b38c03668d4519f0e2c1812faa01cf1bc74bcea48170b002dd53` |
| 数据库行为测试 | `5b9ec5ae909ff6652afaa3a11c4c8449d105662ef9ed98326839cc920000f05e` |
| 当时配置检查器（含共享既有变更） | `037dc4f18fc2aea61311454d3c15c12579d75ff49f26fcd80cf89e5688036589` |

## 真实切换仍需的验收

主工作线执行只读 plan、核对现有 v2 备份与迁移后 apply、verify，将仅两个数据库连接键合并到实际私有环境。迁移任务保留迁移 DSN，API 与审核工作器使用运行 DSN；配置检查器仅对迁移任务放行迁移连接键。最后通过真实无资金业务、运行连接身份与旧表拒绝探针核对，不以隔离通过替代真实切换。操作顺序、恢复边界及凭据失败恢复见 `infra/db/README.md`。

## 真实切换记录

- 迁移前仅v2表备份8.9MiB，摘要和pg_restore目录校验保留在私有运行目录；迁移016已成功应用，实际共有44张v2表、43张业务表。结构定义从001至016共有15份，不能把末尾编号当迁移数量。
- 使用现有项目storage-private网络内的Docker管理任务，依次plan、apply、verify均退出0。新角色无对象所有权、旧表访问或持久DDL，迁移表只读；旧迁移身份、旧表、PUBLIC和默认ACL不修改。第一次verify误在apply结束前发出，返回runtime_role_not_present退出1；等待事务完成后原样verify通过，失败记录保留。
- 新DSN实际连接并通过全部结构及摘要检查，管理属性全部false；私有配置仅替换DATABASE_URL并保留旧DATABASE_MIGRATION_URL，其余键不变，原配置备份0600。首次切换脚本未设置模块搜索路径、随后误用末尾编号作数量分别退出1，均在配置写入前阻断；纠正后退出0，迁移数量15。
- `docker compose up -d --no-deps api review-worker web`退出0。API存活与就绪healthy，实际运行探针显示PostgreSQL16.15、44表、拥有0表、superuser/createdb/public CREATE均false、SSH传输，least_privilege_verified=true。
- 新角色之后的真实桌面管理员登录、分区、专家详情和诊断接口均HTTP200；手机点击另行统一记录。无真实投注及历史数据清理。
- 首次`compose run api`探针因服务固定edge地址已被长期API占用而建网失败；改为在现有API容器执行只读探针，没有改动地址或服务隔离。S3 HEAD成功、受限前缀LIST在3秒读超时，独立保留对象存储尾延迟问题，不能用数据库角色通过掩盖。
