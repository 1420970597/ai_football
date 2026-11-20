# v2 运行角色的受控配置

本工具只在受控Docker管理任务中执行，不在API或工作器启动时自动执行。默认`plan`只读；`apply`创建一个全新、无对象所有权及角色成员关系的运行角色，`refresh`在每次有新v2表的迁移之后补精确授权，`verify`拒绝过宽权限。不会修改旧迁移身份、对象所有者、旧表、全库PUBLIC ACL或默认授权。

## 权限契约

- 角色：LOGIN、NOINHERIT、NOSUPERUSER、NOCREATEDB、NOCREATEROLE、NOREPLICATION、NOBYPASSRLS，密码用SCRAM-SHA-256。
- 当前库CONNECT；public schema仅USAGE，search_path为pg_catalog,public；所有已存在public.v2_业务表仅SELECT/INSERT/UPDATE/DELETE；v2_schema_migrations仅SELECT。
- 不授予CREATE、TRUNCATE、TRIGGER、REFERENCES、所有权、角色成员关系或旧表权限。当前结构使用UUID主键，无序列；若未来出现v2序列，工具拒绝并要求先研究序列契约。
- 不使用ALTER DEFAULT PRIVILEGES GRANT ON ALL TABLES：迁移身份同时拥有历史表，广泛默认授权会把未来旧业务表权限泄露给v2。新增表后必须受控refresh，verify检查所有当前表。
- 数据库PUBLIC默认TEMP保留并报告。TEMP不等于持久schema CREATE；根据本次范围，工具不为禁止临时表而影响其他旧用户。
- 如PUBLIC对任何持久schema有CREATE，或对旧表有读写，或对v2迁移表有写入/业务表有TRUNCATE等权限，工具拒绝；单独REVOKE runtime不能抵消PUBLIC，禁止自动全库撤权。必须另做影响分析，不能降低门禁。

## 受控应用步骤

1. 保留当前v2备份、摘要及pg_restore目录验证；由迁移身份完成当前发行的增量迁移。先只读核对数据库、库名、TLS/隧道、PUBLIC ACL和迁移身份，不能使用本工具迁移数据。
2. 在`.runtime`中准备现有0600源环境引用，包含DATABASE_MIGRATION_URL（优先）或旧DATABASE_URL。此连接只交给管理任务。工具不接受命令行密码。
3. 在**现有项目网络**的受限Docker容器运行下面的`plan`。按实际隧道网络附加必要网络连接，不能在宿主执行Python。项目挂载路径必须与宿主相同，工具只接受项目`.runtime`内的私有文件，拒绝符号链接、宽权限和覆盖输出。
4. 检查ready及表数量后执行`apply`。密码仅生成写入`.runtime/db/runtime.env`（目录0700、文件0600），包含新DATABASE_URL与保留的DATABASE_MIGRATION_URL，绝不打印值。角色及授权在单个事务/专用advisory锁内提交；旧角色不被降权、改密或删除。
5. 运行`verify`。另外用新DSN运行schema只读校验和本人业务无资金探针。若失败，不切换服务；新私有文件保留用于恢复，不盲目重跑apply或自动轮换密码。
6. 主工作线备份实际运行环境后，将新DATABASE_URL及原DATABASE_MIGRATION_URL合并到实际私有配置，不替换其他配置。Compose现有migrate使用后者，API/review-worker使用前者；collector/executor不应获得迁移DSN。渲染后以去敏检查核对角色分离，不能打印完整Compose环境。
7. 统一受控重建/切换，先迁移、refresh、verify，再API及工作器。验证就绪/鉴权/采集入库/审核/只读订单；实际交易维持原关闭状态。最后只读探针确认运行角色非超级、非createdb、非owner、旧表无权限。
8. 回退保留原私有配置与迁移身份；需主工作线明确执行切换，不由工具自动回滚到高权限运行角色。新角色可先撤销新连接但不得删除账本。

命令示例仅含路径，不含秘密：

```sh
docker run --rm --user 0:0 --cpus=.25 --memory=192m \
  --network PROJECT_STORAGE_NETWORK \
  -v /root/ai_football:/root/ai_football -w /root/ai_football \
  ai-football-verifier:v2.0.0-dev \
  python -m infra.db.provision_runtime plan \
  --source-env .runtime/EXISTING_PRIVATE.env --role v2_runtime_app
```

确认plan后将`plan`依次替换为`apply`、`verify`；输出位置默认`.runtime/db/runtime.env`。后续迁移使用`refresh`，要求角色存在且属性、成员关系、所有权安全，不改密码、不接管已有高权限角色。若apply事务失败，凭据文件已私有保存而角色可能尚未创建；主工作线应核对角色存在及事务结果，不能覆盖文件或凭猜测清理。

工具输出只有动作、布尔状态、数量及固定错误类别；数据库连接和SQL异常不输出原异常消息。只读plan不创建文件/角色。所有远端执行及实际配置合并均由主工作线统一处理，本实现阶段只运行临时PG。
