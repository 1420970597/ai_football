# NBA简称和候选范围证据

2026-11-14 UTC；仅修复专家文字已明示NBA的检索和身份词汇。

真实01:30–02:00窗口出现两场NBA简称推荐，检索将NBA按子串展开，混入G联盟、梦幻对垒和2K虚拟比赛；模型扩大为全联赛100条分页，20秒截止前未录入。不是数据库慢查询：实际工具SQL读写各9–69毫秒，主要耗时在多轮模型/候选输入。

以下团队页面于01:49 UTC用Docker内HTTP只读抓取，均HTTP200，并同时出现该队简称、城市及英文球队标识；只取已核验的四个名称映射，不增加泛城市或其它昵称。

|来源|简称→供应方完整中文名|响应SHA256|
|---|---|---|
|https://nba.hupu.com/teams/kings|国王→萨克拉门托国王|64859c6e1206a3d7a22266a82ae33353d74c067a364afd3a6a4d447ae3f988ac|
|https://nba.hupu.com/teams/blazers|开拓者→波特兰开拓者|dd00124dbfae2d727db563f832dd923a2cd157e664d57f061dd6e53ba887463a|
|https://nba.hupu.com/teams/clippers|快船→洛杉矶快船|9e3750553c1840bdfe4b2fdceb454da3d45537a6d0cdbf0f3225454f7657e984|
|https://nba.hupu.com/teams/thunder|雷霆→俄克拉荷马城雷霆|bb3be4315661f87d500817f312a6c2b760d4c42f044cd21005cb98eaf7978711|

官方域名和CDN本次不可获取（404/403），不冒称官方现场核验。供应方当窗只读候选具有对应全名，但来源专家原文不复制到公开报告。

规则：sport=basketball且competition_name精确规范化为nba才扩展简称；NBA G联盟、NBA - 梦幻对垒、VS- NBA 2K23 PANDA独家均不共享该词汇。查询competition_query=NBA必须精确联赛。双方、日期、梯队和唯一性照旧校验；不据盘口是否可售反推比赛。

另发现同名同时间的赛前/滚球双供应方候选。现有source_phase工具字段在工具API旧镜像被忽略；最小恢复已存在的阶段字段及解析规则，仅来源明确滚球等阶段才消歧，不默认选可售候选。实际部署与回归见连续监控记录。
