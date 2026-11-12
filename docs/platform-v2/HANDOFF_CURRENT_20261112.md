# 当前续接入口（2026-11-12）

本入口用于主检出 `fix/TASK-78-hermes-telegram-sync`，避免把旧的未解决交接当作最新网页状态。

> 本轮用户已明确要求覆盖原业务端口并正式上线。正式业务入口已验收为 `0.0.0.0:3003`，域名 `football.5545.eu.cc` 回源同一新版；生产覆盖固定在 `infra/production/web.yml`，幂等启动退出 0、重建容器 0。当前生产状态以 `PRODUCTION_ENTRY_20261112.md` 和 `reports/production-entry-20261112.json` 为准。

> 最新业务变更：首页“专家关联”现在只展示未结算比赛及推荐盘口。源码 `423143b4` 位于 `refactor/TASK-78-web-ux`，API 镜像 `sha256:61acb952…` 已上线，生产覆盖为 `infra/production/api.yml`；web 镜像不变。混合状态场次只返回 pending 盘口，全部结算后移出；“全部赛事”、详情、历史与统计保留。16 项后端专项、三个隔离视口和真实桌面/手机双运动检查通过。权威记录是网页工作树及该已推送分支的 `docs/platform-v2/HOMEPAGE_PENDING_ONLY_20261112.md`、`reports/homepage-pending-only-20261112.json`；主检出 public 模块较旧，禁止从它重建覆盖当前生产 API。

## 已完成的续接阶段

- 最新网页交接及修复证据已在 `refactor/TASK-78-web-ux` 推送；远端引用核对为 `01c4510b0cdfe100d461ce5be7ab077320476d53`。
- 已上线源码为 `26ac5f8cfc9e0690570ad134dd11f94c5c567680`，开发候选 `2.0.0-dev-assets.20261112.1`，web 镜像 `sha256:2fb58c6963503f44d6e5d8f55ee0832eb09b8ff63e64550a3420ecae146bf7e0`。
- 网页视觉重构此前已经上线；本次只解决新 HTML 混用边缘旧 CSS/JS：整组静态资源使用内容哈希目录，相对模块、动态脚本和图片保持同组版本。
- 隔离两代缓存模拟通过；实际镜像 14 屏原基线及买入按钮 18 项通过；常规浏览器 68 项、无障碍 30 扫描/23 检查及压缩边界通过。
- 公网 HTML 与 9 个运行时资源和镜像逐字节一致；新 CSS 首次请求 Cloudflare MISS，一年 immutable；1440/390 各五个游客视图共 10 个组合通过，0 页面异常/资源错误/5xx/横向溢出/写请求。
- 仅 web 重建，其他 9 个同项目服务未变；主检出四份账户未提交文件逐文件 SHA 核对未变。未改资金权限、开关、金额、订单、数据库或其他项目服务。

## 权威记录位置

网页工作树：`.runtime/refactor-wt/web-ux`，其内：

- `docs/platform-v2/ASSET_VERSIONING_20261112.md`：设计、实际命令/退出码、失败修复、镜像和验收边界。
- `docs/platform-v2/HANDOFF_VISUAL_FRONTEND_20261112.md`：已更新的网页交接。
- `reports/web-asset-versioning-20261112.json`：脱敏结构化证据。

工作树不可用时，从已推送分支读取，例如：

```sh
git show origin/refactor/TASK-78-web-ux:docs/platform-v2/ASSET_VERSIONING_20261112.md
git show origin/refactor/TASK-78-web-ux:reports/web-asset-versioning-20261112.json
```

受保护的部署链、原镜像回滚覆盖、原失败日志及真实截图在本机 `.runtime/asset-versioning-release-20261112/`，不得把配置原文纳入 Git。

## 剩余边界与下一步

- 正式业务入口已启用，使用已验证的现有产物；产物如实保留原开发版本编号，不据此声称已创建正式签名包、GitHub Release 或完成整个平台验收。这两条任务分支尚未合并。网页分支包含大量已存在的后端改造，禁止把入口上线当作批准整体合入主线。
- 镜像只保留当前版本资源，旧页面首次懒加载旧模块可能需要刷新；跨版本资源留存未实现。公网浏览器仅验收游客只读场景，不代表登录账户或真实交易验收。
- 个人中心两列布局取向仍未决；Telegram 409 定性与错误分类、unsupported 媒体可见性、Android 当前候选、密钥轮换等继续见原未解决交接。资金套餐/开关/金额与历史跳过策略保持原授权边界。
- 用户最新工作原则：效率优先。聚焦可执行缺口，复用已有通过证据；仅因新改动、失败或未解疑点扩大/重跑检查，不为无关事项阻塞已授权工作。
- 下一技术工作包可优先处理 Telegram 409 的脱敏差异诊断和独立错误分类；先核实现状与所属模块，再按针对性场景验证。不要重新做已上线视觉重构或重复本次缓存修复。
