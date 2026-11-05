# E266：修复 Hermes 网关项目上下文加载

更新于2026-11-05 01:57 UTC。状态：代码已写、针对性与真实框架隔离验证通过，完整冻结验证进行中。

目标：让真实 API 网关加载项目约束，并在启动时阻止工作目录悄然偏离。责任目录为 integrations/hermes、tests、reports。前置依赖为固定 Hermes v0.22.5 镜像及完整预载技能；不改模型、工具权限、消息内容或业务校验。

证据：E265真实会话保存的完整系统提示中有完整技能但无任何项目上下文。真实框架 profile scope 将缺省的 `terminal.cwd=.` 解析为 `/root`，在那里找不到项目 AGENTS；此前独立调用构建器并显式传 `/workspace` 的验证漏掉了网关目录解析。

设计：显式配置 terminal.cwd=/workspace；启动扩展通过框架真实 profile scope 解析目录，要求它与挂载项目根一致，再使用框架构建器验证项目正文完整进入上下文。异常立即阻止启动，错误只报告类别，不输出配置值或正文。技能全文及强制预载保持不变。

实现及验收：独立候选目录编辑，测试正确目录、错误目录、缺失/部分上下文及技能原有失败路径；固定真实框架无网络容器核验真实scope而非绕过scope；冻结完整回归后，在E265完整30分钟结束、自然idle gate持有期间仅更新Hermes配置及启动扩展并重启该服务。其他服务、预算与数据不变。验证新自然审核会话保存的提示正文与完整技能均存在，再开始完整30分钟监控与真实浏览器审核。无法加载或启动则恢复原配置与扩展、恢复原服务并释放gate；不回滚数据。

性能边界：这是此前缺失规则的加载修复，相对实际原提示可能增加token，不能声称去重加速或三秒成功。后续效率分析以真实请求为准。

候选99d3bee：显式terminal.cwd、启动profile目录与完整上下文验证及7项测试。针对性命令 `docker run ... ai-football-verifier:v2.0.0-dev python -m unittest tests.test_hermes_runner -v` 实际exit0；原技能失败路径继续覆盖。固定Hermes摘要9c556157在network=none、独立临时HERMES_HOME、只读候选workspace中实际调用build_profile_terminal_scope及两个启动验证，输出目录/workspace与project_context_complete/mandatory_skill_complete均true，实际exit0。不是手工传目录绕过profile。完整回归使用 `.runtime/e209-live/e266-full-suite.sh`，冻结运行中不修改候选。
