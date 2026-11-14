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

## 14:54 UTC补充完整名称证据

14:50自然发言四组简称首轮均召回正确城市队，但缺已核验全名对应而不绑定。Docker只读获取以下各队页面全部200，正文同时出现简称和城市、标题明确同队；只登记完整中文映射，仍严格限定真实basketball/NBA。不登记泛城市、不推广到虚拟/梯队/女篮。无官方现场证据，延用以上第三方中文身份链；不把候选本身当名称来源。

|来源路径（域名同nba.hupu.com）|简称→完整名|响应SHA256|
|---|---|---|
|/teams/cavaliers|骑士→克利夫兰骑士|9e2e2927dc706c0283c177c2a823f47bbf37fe806b4fa8f0c7153debbeabb28b|
|/teams/mavericks|独行侠→达拉斯独行侠|b819de765a2c968eda51385ec31477e80d2d4bcda99244ab5ef0a473b1a5f3df|
|/teams/grizzlies|灰熊→孟菲斯灰熊|b882465cfa3f5ded97477bb2ca3bc50f1dfa8141608bcb3cfad784410e681045|
|/teams/nets|篮网→布鲁克林篮网|7453c06d08db457c00040ada5569ba8eb345d7a57b4784a52a6b15c75e553368|
|/teams/timberwolves|森林狼→明尼苏达森林狼|1be7c2d434b8d0c705554c34fd3198af7e6d91d0aa7f209ad6e25d511f91021c|
|/teams/bulls|公牛→芝加哥公牛|0622a9cc0f3c741f15b0084d68c515fa2168ad590a43b3a938e34be44ebc1dbe|
|/teams/heat|热火→迈阿密热火|decc10131ce0772e56d39b9a189c697c67f3499b26ea5a9f45376a0aee243823|
|/teams/pistons|活塞→底特律活塞|a5b9d6b34e3ce0509bcf7dcf0d6620a058c76b4683e467a97f930097a9e4a737|
