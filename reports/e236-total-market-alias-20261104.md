# E236 大小球过滤别名

更新时间：2026-11-04 18:35 UTC。状态：设计已记录，尚未实现。

目标：消除真实模型将大小球写作over_under导致的首轮空查询，减少额外工具/推理回合；责任目录services/api/app/routes、tests、reports、docs/platform-v2。前置E234真实558ea5ff工具证据、当前E235正式监控冻结。实际首次market_kind=over_under/selection=over返回0，耗4.794秒；后续model改market_query大小再规范total才找到确切盘口。

设计：只在MarketInput过滤边界将已证明语义等价的over_under规范为现有total，其他类别原样保留，不改盘口存储、期间、线、方向或来源。输出market_filter明确规范化后的total；最终读取和record仍接受原精确quote_id及完整有效性。未知别名仍零项/不回退全市场，不把不同玩法混合，不增加中文/模糊任意猜测，不改模型提示/治理哈希/框架工具白名单。

实现与验收：真实临时PG同一已绑定比赛同时有全场大小/让球/半场大小，别名+期间+方向+线只返回原规范精确选项；未知类别不返回、不授权quote；仍需已绑定、活跃比赛与原权限/报价/完整工具证据。测试先证明旧版别名漏返，再候选原场景通过，冻结定向/全后端后才在当前30分钟结束后部署。不把一个查询恢复当自然延迟达标；失败不放宽匹配或绕过证据。

18:36旧版失败/候选进入验证：真实临时PG单项旧实现1.815秒exit1，期望1项实际0，原失败日志e236-negative.log保留；新增MarketInput单个精确别名validator，仅over_under→total，其他值原样，不改工具/schema或模型提示。原模块reviews/quote_observation/tool_evidence全定向Docker重跑中，代码运行期间冻结，当前未部署。正式E235监控到18:59:32以后不变。

18:39定向通过：93项142.409秒exit0/0失败错误跳过，原权限/停盘/报价时效/取消/并发/完整证据/分页测试保持；别名仅返回同全场/2.75/over的total选项，不读半场或让球，unknown_over_under仍0且不多授权。下一步提交冻结全后端；该更改不动框架/MCP接口/模型提示，固定框架已在父版本E235通过，按必要性不额外重复提供方替身全流程。真实生产仍E235，当前窗结束与全套通过前不切换。

18:43冻结与镜像准备：6bb76e50104f55e8f163d0085ab9cbc7dc6e23b7干净worktree完整Docker后端运行中，源码不改；API候选sha256:9da2bdb1f5046e54eeb5a10fab5369ce5d89150323b292bb637a1084f929275d完整revision一致、受限builder构建exit0、API/worker-only配置check exit0。原定向PG/网络已清理，生产E235不变；即使本窗无新消息也不能宣称自然链路已通过。
