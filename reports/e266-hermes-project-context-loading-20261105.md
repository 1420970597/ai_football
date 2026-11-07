# E266：修复 Hermes 网关项目上下文加载

更新于2026-11-05 02:20 UTC。状态：第二次发布通过、真实会话加载已核实，完整持续运行验收仍进行中。

目标：让真实 API 网关加载项目约束，并在启动时阻止工作目录悄然偏离。责任目录为 integrations/hermes、tests、reports。前置依赖为固定 Hermes v0.22.5 镜像及完整预载技能；不改模型、工具权限、消息内容或业务校验。

证据：E265真实会话保存的完整系统提示中有完整技能但无任何项目上下文。真实框架 profile scope 将缺省的 `terminal.cwd=.` 解析为 `/root`，在那里找不到项目 AGENTS；此前独立调用构建器并显式传 `/workspace` 的验证漏掉了网关目录解析。

设计：显式配置 terminal.cwd=/workspace；启动扩展通过框架真实 profile scope 解析目录，要求它与挂载项目根一致，再使用框架构建器验证项目正文完整进入上下文。异常立即阻止启动，错误只报告类别，不输出配置值或正文。技能全文及强制预载保持不变。

实现及验收：独立候选目录编辑，测试正确目录、错误目录、缺失/部分上下文及技能原有失败路径；固定真实框架无网络容器核验真实scope而非绕过scope；冻结完整回归后，在E265完整30分钟结束、自然idle gate持有期间仅更新Hermes配置及启动扩展并重启该服务。其他服务、预算与数据不变。验证新自然审核会话保存的提示正文与完整技能均存在，再开始完整30分钟监控与真实浏览器审核。无法加载或启动则恢复原配置与扩展、恢复原服务并释放gate；不回滚数据。

性能边界：这是此前缺失规则的加载修复，相对实际原提示可能增加token，不能声称去重加速或三秒成功。后续效率分析以真实请求为准。

候选99d3bee：显式terminal.cwd、启动profile目录与完整上下文验证及7项测试。针对性命令 `docker run ... ai-football-verifier:v2.0.0-dev python -m unittest tests.test_hermes_runner -v` 实际exit0；原技能失败路径继续覆盖。固定Hermes摘要9c556157在network=none、独立临时HERMES_HOME、只读候选workspace中实际调用build_profile_terminal_scope及两个启动验证，输出目录/workspace与project_context_complete/mandatory_skill_complete均true，实际exit0。不是手工传目录绕过profile。完整回归使用 `.runtime/e209-live/e266-full-suite.sh`，冻结运行中不修改候选。

完整冻结结果：1331项613.641秒，报告总616.206秒，实际exit0、0failure/error/skip、committed_source_verified=true，源码99d3bee2e74b5a46dc25b5c3699864eb896d6b96。证据 `.runtime/e266-candidate/.runtime/test-evidence/1983133/backend-tests.json` SHA7288ba3c889f99b53218089f2902a80c6774953c5e477833ccb44c8a8e23c02a。发布只切Hermes，现有API及worker镜像继续保留，预检查合并配置exit0；发布脚本备份旧配置和runner、校验gate与其他六容器，异常恢复原两文件并重建原服务后释放gate。仍需真实新会话确认上下文加载，不能用该隔离结果替代实际模型验证。

首次发布actual exit1：02:09:58.278693暂停、02:10:51.204265自然idle持锁，新服务就绪且live_profile_context_and_skill_verified=true，但直接对Config.Env数组JSON哈希误把顺序变化当配置变化。脚本自动恢复旧config/runner并重建，gate正常释放actual exit0；因此当前未保留修复，不能报告生效。恢复容器be609e79于02:11:58.665687138启动、健康、restart0。用既存E257前置快照的排序环境摘要c3d71295…与恢复容器比较完全相同，镜像和预算也同；其余六容器ID/时间/镜像/预算cmp实际exit0。没有环境值变更或密钥输出。改用排序后的环境摘要、保留首次脚本及失败日志，第二次预检查exit0，重新等待自然idle；不重跑或修改已冻结业务代码。

第二次成功：02:14:48.649565暂停、02:15:52.543803自然idle，部署及gate均actual exit0，已发布源码d064dad。新Hermes容器40c0e9b4于02:17:00.636780135启动、healthy/restart0；实际profile上下文/完整技能验证true，排序环境摘要及镜像/550000000纳CPU/2147483648字节内存逐项相同，其他六容器ID/时间/镜像/预算cmp exit0。无数据变更，领取已恢复。02:18:22.838109产生的真实会话90c30702…保存系统提示21465字符，project_context_present/full_skill_present均true；先前20498字符实际缺上下文，现已修复加载，增加的约967字符不是性能加速。E270完整窗口02:18:14.312173开始，继续自然文图语义、耗时和稳定性验证。
