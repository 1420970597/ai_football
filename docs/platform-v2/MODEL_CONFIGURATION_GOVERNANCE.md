# 模型运行配置治理专项验收

更新时间：2026-10-27。范围：P28-05、P35-05 的模型候选、质量证据、管理员复核与工作器消费闭环。本记录是隔离验证证据，不代表真实语义金标、模型提供方验证或正式版本发布。

## 运行与迁移边界

- 已保存的模型继续作为 migrated_legacy_baseline 生效；没有保存模型时继续使用 Hermes 默认路由。迁移基线的模型修订、技能、框架和工具版本标为 legacy_unattested，不把已有运行配置伪称为已通过新质量门禁。
- 新模型设置只创建候选，返回 application.state=model_candidate_pending 和明确中文说明。设置读取仍显示实际 active 模型，同时返回候选状态；不能把“保存成功”表示为“已生效”。
- 报价有效秒数仍按每次业务操作独立读取。模型候选未复核或治理记录不可解密都不改变有效报价门禁；报价配置本身不可读仍明确失败。
- 复用现有加密管理设置与追加管理审计，不新增迁移、不改变已有表或迁移校验值。治理状态最多 20 个候选、512 KiB；报告最多 256 KiB、1,000 个样本、12 层。达到预算明确拒绝，不能丢弃历史审计来腾空间。

## 管理合同

| 操作 | 合同与结果 |
| --- | --- |
| 读取治理状态 | 管理员独立会话；返回 active、候选、版本及最近消费回执 |
| 建立候选 | 冻结模型、提供方、模型修订、提示 SHA-256、技能版本、Hermes 版本和工具契约；绑定 active 基线及候选摘要 |
| 冻结比较报告 | 核对规范 JSON SHA-256、报告版本、两侧来源、冻结标注摘要、差异编号、覆盖率；报告只能首次写入 |
| 人工复核 | 引用精确报告摘要及候选摘要；每个差异都必须有接受/拒绝及非空核验说明；任一拒绝、遗漏或重复都阻止通过 |
| 激活 | 管理员再次显式操作；检查已复核、报告/来源未漂移、基线未变化、当前管理版本；切换与版本递增及审计在同一事务提交 |

接口位于 /api/v1/admin/model-configuration 及其 candidates、候选 evidence、review、activate 子路径。管理设置页提供同一布局的可折叠候选区，包括来源、报告、差异详情和独立激活确认。界面默认拒绝每一项差异，禁止默认勾选通过。

所有管理写入在取得模型配置事务锁后重新核验敏感会话；CAS 拒绝陈旧版本，单个并发激活只能有一个提交。无管理身份、缺 CSRF、会话已撤销均不能改写治理记录。版本、报告、候选和消费摘要写入既有追加审计。

## 质量证据的事实边界

现有 infra/quality 比较产物明确为 football-review-comparison-v1、synthetic_only=true、automatic_promotion=false。它可以冻结为诊断证据，但状态仅为 synthetic_diagnostic_only，不能复核或激活。

真实报告接收合同为 football-review-comparison-real-v1、synthetic_only=false，并要求：

1. 两侧模型、提示、技能、框架和工具来源与候选/基线一致，来源缺失列表为空。
2. 提供冻结标注摘要、人工标注版本、来源清单摘要、样本数量及 human_annotated、labels_reviewed=true 声明。
3. 两侧同一批样本都完整评估，不允许缺失、输入不一致或未评估记录。
4. 保留差异快照、来源、指标变化及报告摘要；快照自身另有摘要，复核/激活时再次验证。

这些字段是管理员提交的证据声明，不能由 schema 验证自动证明标注真实、模型准确率提高或盈利。专项测试中的此类报告全部是标明用途的合同夹具，没有作为实际金标或候选批准记录写入生产。

新提示摘要必须与当前工作器代码一致；新技能、Hermes 或工具契约版本不能仅靠填字符串激活。当前没有此类新制品部署证明通道，因此版本改变会返回 409 artifact_deployment_unverified，保留候选继续运行旧配置。此限制不会中断现有基线，也不伪造制品部署证明。

## 工作器实际消费

/internal/review/config 只用内部 Hermes 令牌读取非秘密快照，返回配置摘要、模型配置版本和 600 秒有效的签名消费凭证。工作器经既有 Hermes 客户端读取一次并冻结为不可修改对象，核对提供方与本地提示摘要后向 /internal/review/config/ack 回执。模型请求仍只经 Hermes，不新增 API 到模型提供方的直连。

同一尝试复用该冻结对象，既有 hermes_request 轨迹记录对应配置摘要。新激活只影响后续尝试；旧在途快照在有效签名和已知版本链内仍可确认，不会把新 active 版本误标为已消费。重复消费不会重复追加同一模型版本的首次消费审计。

consumed 只表示内部工作器读取并确认此快照；不表示 Hermes 已完成推理、提供方模型支持视觉、技能已部署或推荐结果正确。管理界面明确显示此边界。

## 隔离验证

先冻结 API、测试和质量工具到项目内私有工作区，只将冻结副本只读挂载到容器。临时 PostgreSQL 采用独立内部网络、临时数据、0.5 CPU / 384 MiB；测试器 0.75 CPU / 512 MiB，未连接生产数据库。

实际后端命令：

~~~sh
docker run --rm --name ai-football-model-config-tests-20261027 --cpus=.75 --memory=512m --network ai-football-model-config-20261027 -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/source/services/api:/source/tests:/source -e IDENTITY_TEST_DATABASE_URL=postgresql://postgres@ai-football-v2-pgverify:5432/postgres -v /root/ai_football/.runtime/model-configuration-20261027/source:/source:ro -w /source ai-football-verifier:v2.0.0-dev python -m unittest test_model_configuration test_runtime_settings test_identity_runtime test_identity test_quality -v
~~~

结果：退出码 0，64 项通过，0 项跳过，36.418 秒。覆盖迁移基线、候选不立即生效、TTL 热读取、来源错误、报告摘要错误、合成门禁、差异遗漏、报告快照漂移、非法模型及报告类型、重复候选、陈旧版本、并发激活、撤销会话、授权/CSRF、未核验制品、消费 ACK、在途旧快照和重复回执。身份及原有质量比较测试一并通过。

日志：.runtime/model-configuration-20261027/tests-final2.log，SHA-256：32a3d100a1f0e87d13201fd18927eb0c3ee1038740f8709cb15c0e4bd8a4d882。

实际浏览器命令：

~~~sh
docker run --rm --name ai-football-model-config-browser-20261027 --cpus=.5 --memory=512m --shm-size=128m --network none -v /root/ai_football/.runtime/model-configuration-20261027/ui:/work:ro -w /work ai-football-browser:v2.0.0-dev tests/browser/model-configuration.mjs
~~~

结果：退出码 0，真实 Chromium 在 1440 与 390 像素两种宽度操作通过。使用隔离 HTTP 契约夹具验证候选创建、报告提交、逐项复核、取消激活无请求、显式确认后单次激活、HTML 文本转义及无页面运行错误；这与后端真实 HTTP/临时 PostgreSQL 测试分别构成 UI 与业务合同证据，不冒充生产点击。

同时修复来源信任作者展示：enabled=false 的历史映射保留在 API，但不再列为当前信任作者或显示“移除”；enabled=true 及未携带旧字段的兼容记录仍展示。上述两种浏览器宽度均验证三类记录。

日志：.runtime/model-configuration-20261027/browser-final.log，SHA-256：2e72fc67f65c3f41a869a62de6221dbf8645f9c50e96f1f99bf7ca47013169d8。

本包没有改动审核工作器、工具回放合同、真实账户或交易，也未执行生产部署、Git 发布或正式模型激活。主流程仍需统一全量回归、部署后当前基线消费核验，以及真实标注证据与新制品部署验收。
