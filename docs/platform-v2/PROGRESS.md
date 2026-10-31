# 第二代平台实时进度与验证台账

本轮更新：2026-10-31 17:55 UTC。E129代码提交`8dcb342`后首次冻结全套`sh infra/test/run.sh`运行1021项退出1，唯一错误是`test_fault_recovery_matrix.TelegramDatabaseRecoveryTests.test_real_connection_loss_rolls_back_then_replay_persists_exactly_once`仍mock已移除的`check_lease`。故障注入已改到新的`source_with_lease`事务连接，改动待定向及冻结重验，远端候选未部署。

更新时间：2026-10-31 17:44 UTC。当前目标：每30分钟检查新增专家发言及审核证据；发现错误后修复、Docker验证、部署并重新开始观察。E122–E127观察窗口未通过；E128已部署并通过单群串行/跨群并行的1021项Docker全套，但真实窗口仍有来源degraded和数据库语句超时，尚不能判通过。第8分钟严格审查到的12条当前发言均为短文本闲聊，无媒体或比赛/盘口意图；Hermes完整回执为unresolved，语义上正确，但没有真实推荐可验证命中。实测数据库SELECT往返约245ms、来源查询约491ms，E129 API往返压缩修改已写、测试待跑；远端仍运行E128。媒体历史队列约3337项，Leyu及赛果上游状态仍有退化。

最新目录边界：本项目及用户指定备份目录可写，其他目录只读查找既有实现/有效配置；不修改无关项目或服务。旧备份未删除，经授权复用的运行配置、隧道引用、签名/发现引用和 S3 CA 仅存于项目内受保护且被忽略的 `.runtime/`，秘密不回显、不入 Git。项目容器部署及真实只读联调已经授权，真实资金交易未获授权。

## 当前范围与状态

### E129 — 压缩Telegram租约热路径的数据库往返（2026-10-31 17:55 UTC，冻结测试修复中）

- 目标/责任：在远端数据库高RTT下减少同群消息、目录与游标操作的顺序查询耗时，恢复来源同步和实时入库稳定性；责任目录`services/api/app/routes/telegram.py`及现有Telegram API集成测试。依赖E128已部署并暴露剩余超时。
- 当前证据/根因：E128后最初有一次checkpoint行锁冲突，之后最近日志未再出现持续`LockNotAvailableError`；但`source_for`、`advance_checkpoint`、其他result/fixture接口仍在5秒`command_timeout`内偶发`TimeoutError`。只读数据库探针测得5次`SELECT 1`为245–491ms、来源查找约491ms；同一成功lease/checkpoint路径原先至少顺序执行3–4条SQL。最近`pg_stat_activity`快照10个idle、2个idle-in-transaction、无活动阻塞，故更像多轮远端往返叠加的尾延迟，而非持续锁占用；当前证据不足以断言所有超时均由此造成。
- 设计决策：将活跃来源与owner/lease检查、lease延长合并为单条`UPDATE ... FROM ... RETURNING`；将claim改为由启用来源驱动的`INSERT ... SELECT ... ON CONFLICT`；checkpoint成功advance合并为有owner、lease、旧游标条件的单条原子更新。只在失败路径额外查询原因，保留来源禁用403、lease失效409、游标冲突409、倒退422；不增大超时、不减少群监控并发、不放宽lease fencing。
- 实现动作：目录、事件、作者回补及reconciliation接口改用原子`source_with_lease`；claim单语句；advance单语句成功，失败时再核验lease与source状态。既有隔离API用例验证租约、source、compare-and-swap和reconciliation行为；完整测试、限额API候选、部署及真实窗口待验。
- 验收/失败处理：先运行Telegram API/目录/对账的Docker隔离集成场景，再运行冻结全套。候选部署前确认review任务队列为空，API更新期间保留远端collector并等待新API就绪；部署后重新起算30分钟，查看DB错误、六群健康、作者/管理员同步、新消息媒体与审核回执。若`TimeoutError`仍持续而查询RTT下降，继续检查数据库服务端CPU/IO、pool acquire与桥接耗时，不通过放宽截止掩盖。
- 初始实现检查（17:43 UTC）：`git diff --check`退出0。API代码提交`8dcb342`。
- 冻结首次运行失败（17:54 UTC）：`sh infra/test/run.sh`在`8dcb342`冻结worktree退出1，运行1021项；唯一错误是故障注入测试引用已删除的`check_lease`属性，API行为SQL无失败断言。现改为在`source_with_lease`事务内连接注入中断，回归待重跑。

### E128 — Telegram同一来源checkpoint写入串行化（2026-10-31 17:00 UTC，隔离验证通过，真实观察中）

- 目标/责任：消除同群实时事件、媒体提交、目录同步、检查点和对账并发更新checkpoint租约导致的API 503；责任目录`services/collector/telegram_backend.py`及Telegram transport测试。依赖E127已部署并提供可复现的现场错误。
- 当前证据/根因：E127真实运行约2分钟后，远端`POST /events`、`POST /checkpoints/claim`出现HTTP503。Docker API日志定位到`check_lease`与`claim_checkpoint`获取`v2_source_checkpoints`行锁时`asyncpg.LockNotAvailableError`。另有较早`finish_check`结果核验租约查询超时，属于独立接口，不将其误归为Telegram checkpoint 503根因。Telegram控制状态含6来源、2个TimeoutError与2个reconciliation_deferred，`degraded=true`。严格窗口审计尚无新消息，不能认定审核正常。
- 设计决策：在collector backend中按`chat_id`对所有访问/续期来源租约的API变更请求建立异步锁；同一群事件与checkpoint变更串行，不同群仍可并行。API租约和消息版本验证继续作为最终边界；不重试歧义写入、不放宽超时或租约。HTTP503根据后端稳定`code`记录为受限原因，不记录detail或请求数据。
- 实现动作：更新`TelegramBackend._call`覆盖directory、claim/advance checkpoint、author backfill、events及observed/ack；增加并发行为测试，阻塞同一来源checkpoint请求时证明同来源事件尚未发出而另一来源事件已完成；增加503 code脱敏分类测试。
- 验收/失败处理：定向行为测试通过后提交并冻结完整Docker套件；构建限额collector候选，远端只替换collector并复核bridge/executor不变。部署后重开30分钟窗，检查source_health恢复、同源锁503消失、新消息自动入库并完成Hermes审核、管理员与信任昵称同步、审核回执闭合。若503仍出现，读取受限API错误帧并检查其他非collector调用方/数据库池等待；不得隐藏503或直接认定正常。
- 定向证据（17:01 UTC）：CPU/内存限制Docker运行`ReconciliationTransportTests`共7项，0.018秒退出0；并发测试覆盖同群checkpoint+event串行及跨群并行；503 code只记录固定原因、不泄露detail。`git diff --check`退出0。
- 冻结全套（17:16 UTC）：在不含用户未提交reports目录的detached worktree内运行`sh infra/test/run.sh`退出0，1021项、失败/错误/跳过0，控制台420.779秒；报告`.runtime/test-evidence/1165929/backend-tests.json`标记`committed_source_verified=true`、`source_parent_commit=488ecf5`及`seconds=422.919`。报告已复制至主工作树同路径并`diff -qr`退出0。测试期间被测源码冻结。
- 候选与导入（17:23 UTC）：专用BuildKit容器核验`cpu_quota=50000/cpu_period=100000`、memory和memory-swap各402653184 bytes；候选构建退出0，临时builder构建后移除。初次`docker build --cpus`不受本机CLI支持、退出125且未开始构建；改用带容器资源限制的docker-container BuildKit。候选`ai-football-collector:2.0.0-dev-e128-488ecf5` Image ID=`sha256:768f7403f6c086e3a7b771722bae28f214f18de1c2870f6e84299c35ac28a1db`，OCI revision=`488ecf585906969a1720e1748712913db0ed122d`；构建上下文`telegram_backend.py` SHA256=`b42a1035…`与冻结报告一致。镜像内语法检查首次因只读`/app/__pycache__`失败，改写入`PYTHONPYCACHEPREFIX=/tmp/pycache`后退出0；`docker image save`摘要=`f62b1bdbe85df0c3e00df61db0218a3f07caa75afb43556547ff0e051aae42f2`，经受限SSH导入远端退出0。候选部署脚本已按镜像摘要/revision核对；尚未切换。
- 远端生效/新观察（17:27 UTC）：E127停止后17:26:30.109 UTC租约门禁6/6 inactive；E128单独替换collector退出0，运行镜像摘要和revision均匹配冻结候选，bridge/executor ID、镜像、启动时间前后比较一致。专家/媒体观察器起点分别为17:27:09.680808与17:27:09.559907 UTC，窗口需持续到17:57:09 UTC之后。首轮`/healthz`和`/readyz`均200，Telegram connected/authorized=true、状态starting且尚无来源样本；媒体历史pending=3336，近30条事件有8个TimeoutError。另一次远端状态查询显示Leyu renewal 503、results unavailable；不归因于Telegram修复，但会阻止真实盘口绑定/结算完整验收，需核实是否恢复。
- 观察更新（17:36 UTC）：E128实时采样到13条当前发言、21个期间新增审核任务；严格窗口查询截至17:35:42得到12条当前发言，均为短文本（2–9字）、无媒体，逐条只读检查确认没有比赛/盘口推荐意图；已完成的Hermes任务均有request/result/system receipt且标记unresolved，无工具拒绝或错误匹配，符合闲聊输入语义。窗口中的4个failed任务不在当前这12条发言内；随后两个最新任务仍queued/running。读到另1条新消息晚于该严格查询，随后人工只读确认同样无推荐意图。当前观察仍不能验证真实推荐命中；控制状态六群中有reconciliation_deferred/TimeoutError。
- 数据库退化及E129关联（17:43 UTC）：E128最近7分钟API日志没有新的`LockNotAvailableError`，但发生过`advance_checkpoint/source_for`、fixture写入、readyz和赛果核验数据库TimeoutError；远端媒体对象上传本身约0.2–0.4秒，历史retry批次仍到30秒deadline。只读PG状态10连接idle、2个idle-in-transaction、无blocking PID。容器内只读RTT测量`SELECT 1`稳定245ms、来源查询491ms，支持下一阶段减少Telegram API SQL roundtrip；不代表DB其他查询及平台其他服务已健康。E128观察在E129部署前不作为完整30分钟通过证据。

### E126 — 明确选中截图的意图与来源接口拒绝定位（2026-10-31 16:22 UTC，研究中）

- 目标/责任：让图片中明确选中的盘口及确认单被提取为推荐意图，同时定位两来源历史同步的HTTP拒绝；责任为Hermes独立中文技能、采集后端接口诊断及行为测试。依赖E125新版本继续运行，不提前宣告当前窗口通过。
- 当前证据：E125已新增两张真实图片ready并被Hermes结构化视觉审核，分别168033及28804字节，均唯一识别比赛；第一张源截图明确蓝色选中“上半场大小-附加盘 大0.5/1”，模型却称高亮仅界面元素、无明确推荐，人工查看原图确认属于漏抽取。另一确认单正确抽取大阪樱花+0.5/1。两场目前均已完场，不能补造实时推荐。两来源source_unavailable仅HTTPStatusError，现日志未含状态/稳定原因；16:19复测均转为caught_up或reconciliation_deferred，仍须核实。
- 设计/实现动作：视觉规则区分明确选中/圈画/确认单与未选中赔率列表，并提取期间、方向及复合盘口数值；不要求正文重复图片、不比较原文倍率、不放宽当前可录入判断。接口失败日志仅记录固定路由名、HTTP状态和预设原因码，不记录请求体、原文、凭据、群号或原始错误响应；保持原异常和无自动重试。
- 验收/失败处理：定向规则及拒绝诊断行为测试后冻结全套；新规则实际加载并以相同授权原图核验提取，正式切换后重启30分钟窗口。若仍漏读保留真实结果并继续修复。
- 实现/定向证据（16:24 UTC）：提交26db97e已推送。Docker定向`test_telegram_reconciliation_claims.ReconciliationTransportTests test_telegram_media_scheduling`21项0.246秒退出0，覆盖409原始异常、未知错误/校验输入不泄露、无重试及媒体取消保护。新技能通过既有只读挂载热读取；使用相同168033字节授权原图的历史模式真实模型复核退出0，7873毫秒，准确抽取秋田蓝色闪电对藤枝MYFC、上半场附加大小大0.5/1=0.75、原倍率仅证据，并区分同图未选中列表。未创建生产任务、未读当前盘口、未录入信号；完成后删除临时Hermes会话。
- 冻结全套验证（16:32 UTC）：`sh infra/test/run.sh`退出0，1018项417.856秒、失败/错误/跳过0，报告`.runtime/test-evidence/1079207`；测试期间未修改产品源文件。构建同提交E126 collector，限额CPU/内存后镜像Image ID=`sha256:d918296ee1dfa29d351ce6ba47c3954787e2d964356ace09b61c13925f1fd6db`，OCI revision=26db97e；在SSH远端Docker导入成功，待collector租约交接后部署。窗口临时诊断显示source历史/对账持续deferred，但pending媒体返回中错误原因呈TimeoutError/HTTPStatusError，E126加入脱敏拒绝分类便于准确定位；不对旧消息批量改状态。
- 远端生效/新窗口（16:36 UTC）：16:35:16只读租约门禁6条均到期；Compose只替换collector并等待启动退出0，运行Image ID/revision与候选一致，bridge/executor ID、image、StartedAt前后cmp相同。新观察器从16:36:08.495 UTC起连续采样。初始Telegram控制请求短暂ReadTimeout/连接初始化中；媒体pending 3348、近24小时30条中20 TimeoutError、ready本窗0，仅为起点不构成功能通过。接下来核对来源返回原因及本窗原文/图像任务终态。

### E127 — Telegram来源启动时租约先于管理员目录同步（2026-10-31 16:39 UTC，代码已写）

- 目标/责任：新collector启动后先取得远端来源租约，再自动同步管理员/专家目录和历史；责任为`services/collector/telegram.py`及collector回归测试。前置证据来自E126的脱敏远端拒绝日志。
- 当前证据/根因：16:35:55–16:36:20 UTC，新collector首轮多个`PUT /directory`均HTTP409 `lease_inactive`。`_sync_source`当前顺序是`_refresh_directory`后才POST checkpoint claim；directory API本身要求租约有效，故每个新进程启动会先被拒绝，至少延迟一次五分钟目录刷新。日志还记录过HTTP503空响应，可能为限流/桥接不可用，尚未确定。
- 设计/实现：每个群先在独立短临界区claim/续租，再释放群锁执行管理员目录查询与同步；保持慢目录请求不阻塞实时消息。增强安全拒绝分类：HTTP503空响应记为有边界的桥接/API不可用，不记录响应体或请求数据。
- 验收/失败处理：更新慢目录并行测试，明确证明directory调用前已完成来源checkpoint claim且实时消息不等待目录；Docker定向及冻结全套回归，通过后构建/导入/单collector部署并重新起算30分钟。仍有503时只读核验桥接/连接池，不绕过租约，不盲重试有歧义请求。
- 定向证据/失败记录（16:42 UTC）：Docker禁网6项通过，含“先claim后directory但慢目录不持群锁”和HTTP503空响应安全分类测试。首条定向命令误用测试类名`TelegramCollectorTests`，命令exit1、测试进程启动前因类名不存在；按仓库实际类名`CollectorTests`立即修正并重跑全目标成功。E126窗口严格当前消息3条：2条仅图片/不支持媒体（media_state=unsupported、没有送审任务），1条ready相册、2张图片进入Hermes，终态unresolved并有完整搜索/模型/系统回执；本窗口尚不足30分钟且来源健康仍degraded，不判通过。
- 冻结全套（16:51 UTC）：提交c8fe497完整源码未再更改，`sh infra/test/run.sh`退出0，1019项420.897秒、失败/错误/跳过0，报告`.runtime/test-evidence/1118640`。只读API日志佐证既有503主要是`asyncpg.LockNotAvailableError`及语句`TimeoutError`；pg_stat_activity抽查13连接idle ClientRead、当前无持续阻塞。
- E127候选（16:52 UTC）：CPU配额50%、384MiB内存构建退出0，Image ID=`sha256:b3131f6a523e6797ae2f2345a8f5fe3e0db136165b9af62948a2c6c6066d4245`，OCI revision=c8fe497；采集器telegram.py源码摘要fa9aceac…与冻结构建上下文一致。待远端导入及自然租约交接后单独部署。
- 远端生效/观察（16:57 UTC）：E126采集器停止后租约门禁读数为6条全部inactive；未强制清除租约。E127单独重建collector退出0，运行镜像ID与revision=`c8fe49763decf135b51ce99b3a78bcc9067336e7`一致，bridge/executor容器ID、镜像及启动时间前后cmp相同。专家与媒体观察器分别从16:57:40.567201和16:57:40.567049 UTC起运行，完整窗口终点不早于17:27:40 UTC。初始Telegram已授权连接，API健康/就绪200；来源状态仍在starting，媒体pending共3339且近30条仍有11条TimeoutError，均待本窗持续验证。E126末次样本为来源degraded、3条对账延期；该窗口被版本切换终止，不通过。
- 观察终态（17:06 UTC）：E127观察实际只运行约7分12秒，于行锁503复现后终止，不满足30分钟。API日志再现`ingest_event/check_lease`与`claim_checkpoint`上的`LockNotAvailableError`；来源仍degraded，另有4条对账延期。严格窗口只发现1条当前图文消息，Hermes image_count=1并留下完整请求、结果和system receipt，最终unresolved且无信号；短文本/图片意图需结合真实输入人工核验，不能把该样本称作正确推荐。媒体历史队列约3340条，历史回补中仍有批次截止；不作为当前新消息审核通过证据。

### E125 — 媒体事件原子租约提交与合理截止（2026-10-31 16:04 UTC，代码已写）

- 目标/责任：消除媒体登记与最终回写在5秒提交截止上重复失败；责任为采集器媒体提交及行为测试，依赖E124阶段日志。
- 当前证据：E124在16:01:21 UTC完成19182字节真实图片下载（789.9毫秒），16:01:31 UTC live媒体以TimeoutError退出，总10793.2毫秒；没有uploaded日志，符合候选提交5秒失败后failed提交再5秒失败。S3原图幂等上传200（5478毫秒）及API同事件回写200（3903毫秒）分别已有证据。服务端events在事务内已检查并续租；额外claim没有增加保护。
- 设计/实现：媒体提交直接调用events，由服务端原子租约、版本、信任作者及删除规则做最终判定，不再单独claim或持有群组锁；截止9秒，低于远端桥10秒整体预算。传输20秒、批次30秒及重试状态保留不变。
- 验收/失败处理：新增无checkpoint缓存时只做事件提交不发重复claim；保留失效租约服务端拒绝、迟到上传、删除/编辑防复活和并行上限回归。定向测试后完整冻结验证、部署唯一collector、重置窗口；若仍超时，继续测事务耗时及远端桥状态，不放宽租约或伪造ready。
- 验证/准备（16:11 UTC）：17项媒体回归0.221秒退出0；提交24e2d5f372c652cf04bcf85291e2e2859fbc5b86已推送。冻结全套`sh infra/test/run.sh`退出0，1016项422.733秒、0失败/错误/跳过，证据`.runtime/test-evidence/1039534`。候选限额构建并SSH导入退出0，Image ID=`sha256:5c32bff80f68027bba8c0233bc461f0bab5dad0face8e31baec111dc2b917f7c`，revision=24e2d5f，源码摘要76ffc198…与构建上下文一致。只读pg_stat_activity未见锁阻塞，3个客户端等待事务最大2.518秒。E124实际上传日志已有4张、S3 v2对象由5增至9；仍有回写失败，故不能记图片审核恢复。仅验证原图并尝试受控ready恢复的探针两条均NoSuchKey、未执行事件写入。

- 远端生效/观察（16:15 UTC）：旧collector停止后只读租约门禁16:14:23 UTC确认6来源active=0；`sh .runtime/e117-20261031/ssh.sh < .runtime/e125-20261031/start-collector.sh`退出0，新collector Image ID/revision与冻结候选一致，bridge/executor容器ID、镜像与启动时间前后cmp不变。专家及媒体观察起点分别16:14:53.817302和16:14:53.817908 UTC，窗口1800秒。初始Telegram connected/authorized均true，API健康/就绪200，但新窗口无消息、无原图ready，因此不能记业务通过。原图恢复探针继续只验证真实对象并通过现有版本/租约接口提交，不伪造事件或信号。

### E124 — 媒体候选重复登记和回写截止（2026-10-31 15:51 UTC，代码已写）

- 目标/责任：减少每次媒体重试的数据库往返，使真实图片能够在截止内完成；责任目录`services/collector/`和媒体调度测试，前置依赖E123已部署。
- 证据/缺口：使用已存在原图原键、同内容SHA256及元数据进行一次幂等S3上传返回200、5478毫秒；没有新增测试对象或推荐。对既有pending事件完全相同状态的API幂等回写返回200但3903毫秒；现有5秒提交截止还包含一次额外checkpoint网络往返，且已有候选每轮重复登记。30条事件经Pydantic规范化与持久内容比较字段差异0。
- 设计/动作：完全相同的候选证据已持久登记时不重复POST；同键证据不一致立即拒绝。媒体提交复用既有10秒内checkpoint缓存，超过缓存才续租；最终事件API仍每次检查实时租约、消息版本及删除，不放宽服务端校验。新增下载/上传阶段耗时日志，仅字节数和毫秒、不含原文/ID/对象键。
- 验收：新增已登记候选重试仅一次最终ready提交，以及checkpoint缓存仍被服务端失效租约拒绝；随后全套冻结、构建部署并重新起算30分钟。E123当前5分钟仍无新图片，不能据此宣称图片恢复；原观察器继续保留证据至修复生效。
- 隔离验证与候选（15:58 UTC）：定向16项0.227秒退出0；提交c52ffae594f08c99256ad713cd5e92c405b89e2a已推送并核对远端引用。冻结`sh infra/test/run.sh`退出0，1015项419.546秒、无失败/错误/跳过，证据`.runtime/test-evidence/1012847`；测试期间未修改源码。候选镜像`sha256:b77553b09a3bb647e889174a51abd2683e8e7067abb2ef54b5b815c9b74a67ba`限额构建及SSH导入退出0，revision=c52ffae，源码摘要84466b41…与构建上下文一致。下一步仅collector交接、租约自然过期及重新观察。
- 远端生效/观察（16:01 UTC）：15:58旧collector停止退出0，16:00:17 UTC六来源租约active=0。远端仅collector按内容ID更新退出0，运行revision=c52ffae，bridge/executor ID/镜像/启动时间cmp不变。新30分钟专家观察从16:01:05.614 UTC、媒体观察从16:01:05.367 UTC开始，完整终点须在16:31 UTC后。初始控制connected/authorized，来源同步尚初始化；媒体队列3345、尚无本窗原图，不算功能通过。E123观察器运行期间被E124替换，因此不记为单版本完整30分钟成功证据。进度提交65405e5已推送并核对远端引用。

### E123 — Telegram媒体重试并行化与S3发送兼容（2026-10-31 15:32 UTC，最终隔离验证中）

- 目标/责任：使近期图片重试不再被批次首个慢请求独占30秒截止；责任目录为`services/collector/`和采集器测试。前置依赖是E122待媒体排序已部署。
- 当前证据/缺口：15:08–15:20 UTC批次30条均为近24小时消息，但仍有13–14个TimeoutError，窗口内ready媒体为0。源码中批次逐条串行；尚不能证明上游Telegram/S3本身正常。E122窗口未通过，部署后重新起算30分钟。
- 设计/实现：最多2个重试并行，保持群组锁、全局媒体槽、消息版本、租约和删除保护；批次取消/异常时取消并等待全部子任务。不存在来源或处于退避的项直接返回，历史记录不删除。
- 验收/命令：新增3条图片的行为测试，两个慢上传同时开始、第三条等待槽位，最终三条均ready；Docker禁网定向14项退出0（0.212秒）。下一步冻结全套测试、构建同源候选，仅切换远端collector并验证真实图文审核；失败继续定位上游或回写错误，不将其误称赛事不匹配。
- 失败记录：首次进度补丁因长上下文不匹配未写入，改用小锚点重试；产品代码定向测试已通过，尚未部署。
- 新证据/设计修订：远端S3只读首探针认证成功但列表超时（退出1）；按collector实际环境复测HEAD/list均200（8–490毫秒），抽样候选HEAD 404，v2图片对象仅5个、最新14:25 UTC。旧项目归档只读显示S3v4及path寻址；当前SDK发送前拦截证明默认put_object自动采用aws-chunked、CRC32 trailer、STREAMING-UNSIGNED-PAYLOAD-TRAILER且无Content-Length。为现有网关改为path/S3v4、payload_signing_enabled=true、request_checksum_calculation=when_required；仍保留TLS/原CA、完整SHA256签名与本地内容哈希，不向桶写入测试假对象。
- 验证：并发修复第一次冻结全套1012项、419.032秒、退出0，证据`.runtime/test-evidence/954013`。随后新增上传兼容修复，旧全套不能代表最终源码；真实SDK发送前回归验证固定Content-Length、无Transfer-Encoding/Content-Encoding/trailer、SHA256为完整图片摘要，结合媒体调度15项0.516秒退出0。第一次单测命令误用类名TelegramTests退出1，改为CollectorTests后通过。最终冻结全套须重新执行；尚未部署，真实上传恢复仍未验收。
- 最终冻结验证/候选（15:40 UTC）：提交a47b9459110f7bd8b124125fc094d273d79666f0已推送且远端引用一致。`sh infra/test/run.sh`退出0，1013项、426.782秒、失败/错误/跳过0；证据`.runtime/test-evidence/976326`报告429.136秒，父提交a47b945；测试快照telegram.py摘要a5450640…与源码、候选容器逐字一致。限额Docker构建退出0，Image ID=`sha256:960a4cd712d55d7c4bf335849d29940be0082595c20e895fcdf9eae71a89a502`，OCI revision=a47b945；受保护SSH导入退出0，未发布registry正式资产。仅collector部署覆盖使用该内容ID，bridge/executor保持原实例。停止旧collector退出0；15:40:30 UTC六个租约尚有效、最晚15:41:57 UTC，待自然过期后启动唯一新实例。
- 远端生效（15:43 UTC）：15:42:45 UTC只读门禁六来源active=0，远端Compose仅collector `up --no-deps --no-build --pull never`退出0；运行Image ID/revision与候选一致，bridge/executor容器ID、镜像及StartedAt前后cmp相同。新观察器从15:43:25.565 UTC开始，须运行到16:13:25 UTC以后；首样本控制HTTP200但Telegram尚未完成授权连接初始化，不作为恢复通过。S3只读探针HEAD/list200，当前5张v2图片，等待实际采集上传。进度提交f391e5b已推送且远端引用一致。

### E122 — Telegram近期图片消息被旧媒体积压饿死（2026-10-31，部署后观察中）

- 窗口终态（15:38 UTC）：专家观察器退出0，实际1849.820秒，27条实时发布时间消息、79条总入库（含补扫）、44任务unresolved、17系统回执、0绑定/信号；未审核当前/编辑12条为宽口径，包含不支持媒体及被删除/修订项，不能当12条漏审。严格当前未删除版审计确认图片未ready且无任务，故窗口失败。媒体观察器实际1850.581秒退出0，窗口待媒体2、其中failed1、ready图片0，批次30项近期、12个TimeoutError。Telegram仍connected/authorized但degraded。

- 目标：让实时窗口内等待上传/重试的图片发言进入媒体重试批次，避免历史积压占满全部名额；保持历史数据完整及历史消息不自动触发模型审核。
- 责任目录：`services/api/`、`tests/`。前置依赖：当前已部署Telegram采集器通过API拉取待重试消息；排序字段以采集到的原始`posted_at`为准。
- 证据/缺口：14:34:47 UTC容器内只读查询显示3290条`pending/failed`媒体事件、其中4条近24小时；`/internal/telegram/pending?limit=30`原来只取`updated_at`最早批次，0条近期消息入批。未完整媒体不得送Hermes，因此这解释了近期推荐消息未被模型看见，但仍须观察部署后是否完成上传、生成审核任务与真实绑定。旧观察器14:14:47–14:45:26 UTC运行1839.187秒，终点健康端点均200但Telegram仍degraded，当前/编辑未审核12条、unresolved 35、accepted signals 0；有4条赛事绑定但无记录信号。摘要显示两条较长且命中推荐词的消息仍为pending/failed；已送模型的消息多数原文1–7字符且无推荐特征，不能据此判为赛事命中失败。
- 设计决策：SQL先按原始发布时间是否落在`REALTIME_WINDOW_SECONDS`内分桶，实时消息在前；每个桶仍按`updated_at,id`排序。超过实时窗口的积压仍在分页批次中FIFO轮转，不删除、不自动审核。
- 实现动作/验收：API待媒体端点已按原始发布时间分桶，实时窗口在前、两桶各自仍按`updated_at,id`顺序。Docker隔离回归构造31条历史媒体消息和1条近期推荐消息，验证单批30项中近期消息优先、历史项仍FIFO。完整冻结后端套件通过后，构建限额候选、复核队列门禁，部署API及review-worker并重启观察窗。
- 失败处理/当前状态：若近期消息进入批次但在采集器30秒批次截止内仍反复超时，继续检查单消息传输/回写截止与分组调度；不能把旧消息标为已处理，也不向不完整媒体创建模型任务。先在15:00:42 UTC API部署前门禁全0并停止旧worker；API随后产生1个queued任务，以E121 worker自然排空，再于15:04:18 UTC停旧worker后复核queued/retry/running全0，启动E122 worker。15:05:48 UTC API真实待媒体端点200、批次30项且近实时30项；15:08:05 UTC队列总3313、近实时入批30、其中14 TimeoutError/2 other，ready近实时媒体仍少。新30分钟观察窗尚未通过。
- 已执行命令/退出码：只读调查`.runtime/e121-media-queue.py`、`.runtime/e121-window-diagnostics.py`、`.runtime/e121-intent-summary.py`（Docker内）均退出0；截至14:34:47 UTC队列摘要为3290总待重试、4条近期。Docker具名隔离网络和临时PostgreSQL定向测试退出0（1项，1.963秒）；冻结完整`sh infra/test/run.sh`退出0，1011项、0失败/错误/跳过、427.110秒，证据`.runtime/test-evidence/876762/backend-tests.json`，快照源父提交`f31b078`、`committed_source_verified=false`；快照中的`telegram.py`与`test_telegram_api.py`SHA分别为`6b1a6ddc…`和`9b81e12b…`，与冻结工作树匹配。`jq`因宿主机未安装退出127，首次Docker JSON解析镜像引用摘要格式无效退出125，改用既有验证器镜像解析退出0。代码提交并推送`f41f3d3`及进度提交`88ddcc1`，远端引用核对一致；`git show --check`、Compose `config --images`退出0。限额Docker API镜像构建退出0，Image ID=`sha256:ceb00bd2f495a792676e9639c55deb831954ca95dc181ae580978086794126da`，revision=`f41f3d332d402e6c3247d94c3dfbb75df359df0c`；镜像内路由SHA=`6b1a6ddc…`。API与review-worker现为该镜像且healthy/running；API `/healthz`、`/readyz`均200；collector和Hermes未重启。只读观察器`.runtime/e120-watch.py`与`.runtime/e122-20261031/media-watch.py`分别从15:07:44及15:08:04 UTC在API Docker容器启动，完整窗口终点应在15:37 UTC后。

### E117 — Telegram 发言补扫、原文图片预览与既有乐鱼配置复核（2026-10-31，最终回归重跑中）

- 目标：在不回退群组检查点、不重复模型审核/交易的前提下找回已监控作者近期漏采原文；在发言列表直接预览授权图片；全局乐鱼协议配置更新后，允许用户为被明确拒绝的旧账户发起一次受控无资金复核；部署后观察真实 Telegram 接收至少10分钟。
- 责任目录：`services/api/`、`services/collector/`、`web/app/`、`services/api/migrations/`、`tests/`。前置依赖：现存远端 Telegram worker、授权群组会话、S3只读图片链路、项目现有账户专属执行器。资金下单保持关闭。
- 运行只读证据（Docker）：API/Web为`e116-5206063`且healthy；Telegram控制端HTTP200、connected/authorized=true、6个来源。当前来源状态含2个`catching_up`、4个`reconciliation_deferred`，状态`degraded=true`；不等于消息轮询停止。数据库6个群组租约均有效；近一小时三组分别入库24、26、74条消息。另三组当前目录状态是`member_enumeration_restricted`，其中至少一组近小时仍有26条消息，故成员枚举失败与消息采集是不同状态。检查点约在04:55–04:59 UTC，最近成功控制同步05:01:55 UTC，采集时未打印原文、群名或凭据。
- Telegram根因证据：冻结代码`_stage_group`在写事件前将`sender_id`不在`trusted_sender_ids`中的作者直接返回`ignored_author`；主群组检查点随后仍前进。新增普通成员或专家信任映射不会安排从已经越过的群组水位按作者补读，导致用户看到消息但系统永不保存。4个来源的当前reconciliation有界延迟，仍需实时监测是否恢复；首次按`created_at`筛选的表结构探针语句在Docker执行超时退出1，保留失败记录，已改用checkpoint及事件表可用字段完成主要只读核验。
- 图片预览根因：`/public/experts/{id}/messages`和管理员消息列表已返回有短时授权grant的`media_objects[].url`，详情窗通过带`X-Session-Context`的Blob读取并呈图；列表卡片只渲染文字和图片张数，没有缩略图控件。
- 既有乐鱼账号根因：受限查询只读到一个当前非disabled账户，状态`health_state=rejected`、`renewal_state=rejected`，最近验证任务`verify/rejected/app_login_rejected`创建于2026-10-30 04:25:47 UTC、更新于04:26:13 UTC；其后乐鱼`leyu_app_host`于19:41:47 UTC及`leyu_app_token`于19:41:42 UTC修改，至本次检查没有新账户任务。用户校验路由对`renewal_state in (unknown,rejected)`一律409拒绝；旧账户私有会话中的`leyu_app_host`也会覆盖当前全局主机配置。全局`leyu_app_token`不作为个人账户令牌复用，保持个人账户与采集账号隔离。
- 设计决策：为当前启用的作者关系存储单调、持久的最近7日补扫游标和固定群组水位上界；先持久每条消息，再推进作者游标，补扫只读Telegram且复用事件去重/媒体重试；旧消息沿用实时窗口门禁，只有真实新近消息可自动审核，不降低群组检查点。来源轮询遇到成员目录限制仍继续读消息。图片预览复用短时grant、同源Blob和当前user/admin上下文，限每次展示12张、单图6MiB、允许的图片MIME。乐鱼只对最新任务为明确`app_login_rejected`且登录协议配置在拒绝后更新的账户开放用户主动复核；`unknown`和未更新配置的拒绝继续阻断，不把全局采集令牌注入个人账户，复核会清空旧私有会话并关闭自动投注。
- 实现与验收动作：新增向后兼容增量迁移以及author-backfill内网API/采集消费；点击信任作者触发该作者的7日受限补扫，支持进程重启后继续；发言卡预览授权原图缩略图并可进入完整原文；校验接口在协议版本确实更新后将原凭据排队无资金复核、保留速率限制/审计并清除不适用的旧rejected会话状态。验收包括并发/重启/分页重复、检查点单调、未信任作者不触发LLM、图片上下文越权拒绝及手机/桌面、更新前拒绝/未知拒绝拒绝复核、更新后允许一次并保持auto-betting关闭。
- 真实监测验收：部署同源API/collector后记录开始结束UTC、每来源检查点和消息/媒体计数、任务失败类别、Telegram会话状态；10分钟内检查点应持续刷新，出现新Telegram消息时其作者消息及媒体需在检查点前持久。若窗口内没有新聊天消息，只能证明采集活跃和检查点前进，不能声称验证了新消息内容一致性；保留该局限。
- 阶段命令记录：工作树/分支核验`git status --short --branch && git log -4 --oneline --decorate`退出0，工作树干净、分支`fix/TASK-78-hermes-telegram-sync`、HEAD `3773f53`。实际API库只读SQL查询退出0；Telegram控制状态查询退出0。两次只读SQL探针分别因shell引号构造退出2、一个较重分组查询超过远端5秒查询限额退出1；未执行写操作。下一步为编写增量迁移和行为测试，先在临时Docker PostgreSQL验证迁移，再执行定向回归。
- 实现及首次浏览器验证（2026-10-31 05:22 UTC）：新增023只增量迁移、租约保护的倒序作者游标API及受信作者加入时的7日补扫任务；采集器每来源每轮限10条，事件提交后才CAS推进，主检查点不回退。消息卡以当前角色grant请求授权缩略图，懒加载、限额并能点开完整原文；个人账户主机从当前受保护协议模板读取，旧拒绝账户只有配置更新时间晚于明确登录拒绝时才由本人排队校验，未知结果仍拒绝、自动投注许可关闭。Docker Playwright `sh tests/browser/check.sh`退出0，31项通过；新增列表缩略图加载及点击详情断言通过，证据`.runtime/browser-ui-3838338/results.json`。`git diff --check`退出0。尚未完成数据库/业务隔离回归，尚未部署。
- 首轮完整回归失败及修复（05:30 UTC）：`sh infra/test/run.sh`退出1，999项，3 failures/5 errors，报告`.runtime/test-evidence/3841712/backend-tests.json`。全部错误为作者信任写入中BIGINT `$4`与整数常量推断歧义；修复参数显式BIGINT转换。3项迁移断言未包含023，已更新并增加迁移水位/频道身份/重复应用专项测试。其他失败未见独立业务根因。
- 针对性隔离回归通过（05:33 UTC）：具名Docker PG网络`ai-football-v2-e117-target-net`和容器`ai-football-v2-e117-target-pg`中运行`test_telegram_collector test_telegram_api test_telegram_sources test_telegram_reconciliation_claims test_migrations test_account_state`，133项、88.264秒、退出0；数据库及网络随命令清理。覆盖失败后游标不前进、作者API租约/CAS、普通成员信任后水位任务、023保留检查点、账户配置更新时间门禁、旧会话清除、本人触发和未知状态阻断。该定向运行后源码未改动。
- 全量与浏览器回归通过（2026-10-31 05:45 UTC）：冻结源码`sh infra/test/run.sh`退出0，1000项、411.571秒、失败/错误/跳过均为0，证据`.runtime/test-evidence/3869743`；`sh tests/browser/integration-check.sh`退出0，覆盖API及桌面/手机25个场景，合成订单写入调用为0，证据`.runtime/web-integration-3887593`。运行中未修改源码。此前`sh tests/browser/check.sh`31项及定向Docker回归133项亦通过。此时持久库迁移和服务部署仍未执行。
- 发布/远端部署门禁复核（05:46 UTC）：候选工作流会创建不可变开发预发行并推送六个镜像；候选策略文档要求准确源码tag和全量验证。远端worker runbook要求备份后增量迁移、同源摘要镜像、私有链路/S3检查、零本地及远端worker和租约交接；manifest观察缺项时必须停止。`infra/db/README.md`要求受控Docker内核对现有迁移身份和TLS，备份及pg_restore目录核验后才迁移。尚未创建tag、提交、推送、发布镜像、写远端业务配置或迁移数据库。
- 迁移前备份与恢复验证（05:56 UTC）：通过现有 Compose `migrate` 容器安全提取受保护迁移连接，再在 Docker PostgreSQL 16 固定摘要镜像中导出全`public` schema结构和全部`public.v2_*`数据；未停服务、未写业务库。备份保存在忽略目录`.runtime/e117-20261031/`，文件0600，SHA-256校验通过；`pg_restore --list`分别有338及61行有效清单。无网络临时 PostgreSQL 磁盘卷完整恢复退出0；恢复库为022、6个来源检查点、10条专家映射、11,898条专家消息。同期线上只读快照为022/6/10/11,910，12条消息差异符合持续采集期间的新写入。恢复中先出现512MiB tmpfs空间不足、仅v2表schema遗漏触发函数、默认public schema冲突三次隔离失败；均保留原备份，改用完整public结构和临时Docker卷后成功。专用恢复容器/卷已移除，远端数据库未改。
- 数据库权限与传输只读核验（06:03 UTC）：固定Docker API镜像内运行运行角色`plan`和`verify`均退出0，迁移角色未改、46张v2表无`PUBLIC`/旧表越权、运行角色非owner且持久DDL关闭；运行API数据库只读查询证明PostgreSQL 16、非超级用户、无schema CREATE、无v2表所有权。协议为`DATABASE_TRANSPORT=ssh`且目标为既有`storage-tunnel`，隧道容器healthy，因此PostgreSQL会话`pg_stat_ssl=false`是SSH隧道传输，不按TLS传输误判。只读S3探针通过HTTPS/CA桶认证及`v2/`单对象上限列表，返回当前无对象；写和删均未调用。Telegram控制状态HTTP200、connected/authorized、6个来源；4个reconciliation延迟、2个追赶，6个持久来源租约当前有效；状态仍degraded，需切换后继续观测，不将其记为健康。
- 现有原图与新上传前缀核验（06:07 UTC）：数据库有2,266条专家消息引用媒体；只读S3 `v2/`列表返回空，但从一条现有数据库引用进行HTTPS/CA `HEAD`成功，证明既有原图仍在旧`telegram/messages/`命名空间。collector原常量会把后续补采继续写到该旧命名空间，与远端限定`v2/`前缀冲突。将新上传键改为`v2/telegram/messages/`，旧引用仍按数据库授权读取，不移动或删除历史对象；新增mock上传路径断言，禁止触碰真实桶。
- 最终链路检查与冻结（06:11 UTC）：发现远端桥未放行新增`PUT author-backfill`，主动停止当轮全套测试（退出137，未作通过证据），补精确collector白名单、executor/错误方法/多余查询拒绝与真实backend调用契约。新增媒体测试首次缺`os`导入退出1，补全导入后Docker禁网专项73项、8.440秒、退出0；规划检查46包/337动作/266有证据项退出0。只读SSH核验远端collector/executor/bridge均为51e20ee同源摘要镜像、每类唯一；私有env0600、socket目录2750及socket0660、执行器实盘false。当前实现准备分阶段提交推送，后续完整回归必须以该新冻结源码重跑。
- 实现提交及最终后端回归（06:21 UTC）：`fe8f5df0c60a13e38bfb58a1c8107fdaf18170f8`提交并推送任务分支，`git ls-remote`一致；新增差异常见敏感模式匹配0、暂存差异检查退出0。该干净提交执行`sh infra/test/run.sh`退出0，1002项、407.417秒、无失败/错误/跳过，证据`.runtime/test-evidence/3964796`。`v2.0.0-dev.20261031.1`指向相同完整SHA，候选流水线41175711866在截图门禁失败，未发布镜像或Release。本地`sh tests/browser/visual-check.sh`复现退出1，14屏仅admin-guest的390/1440两屏失败；逐屏查图确认差异来自E116按用户要求删除MFA说明，旧截图未更新。仅复制已审两屏并更新其manifest SHA，阈值不变；正在重跑截图检查，应用源码未变化。原标签保持不可变，不删除或移动。
- 截图/规划冻结检查（06:22 UTC）：`sh tests/browser/visual-check.sh`现退出0，14屏通过；Docker `python tasks/check_planning.py`退出0（46工作包、337动作、266个有证据完成项）；`git diff --check`退出0。变化仅两张已审基线图片及manifest，尚未提交。下一步提交并推送基线修订，创建新的日期序号候选标签，等待全量后端/浏览器/Android验证通过后才取镜像清单。
- 基线提交及候选验证（06:26 UTC）：截图基线修订提交并推送为 `65e62133ef98e48e81dace961a4c15568ebe9713`，分支与 `origin/fix/TASK-78-hermes-telegram-sync` 同步；不可变标签 `v2.0.0-dev.20261031.2` 指向该完整提交。候选工作流 `41176346833` 的 policy 已 success，backend、browser、android仍为in_progress；Android独立边界工作流 `41176308068`此前success。第一版状态查询命令因镜像摘要引用拼写错误而未启动容器，使用仓库固定镜像摘要重试后成功取得GitHub作业状态。尚未下载候选资产、未更新远端worker、未迁移生产数据库。
- 候选作业及部署前采集基线（06:30 UTC）：候选 `41176346833` 的 policy、browser、android均success，backend仍运行，镜像未发布。通过现有远端SSH容器只读检查，collector/executor/api-bridge均运行旧修订`51e20ee8e256b94bf8947c4e1be5a86c52f90b03`，executor实盘开关false，socket目录/权限符合02750与0660。数据库尚处022，且没有作者补扫列。06:27–06:29 UTC只读Telegram快照显示会话connected/authorized，6/6来源租约有效，检查点持续推进；控制状态为2个catching_up、2个reconciliation_deferred、2个unavailable（错误均为TimeoutError），状态degraded=true；约1分钟窗口入库3条新消息、未见新图片。此为部署前基线，不满足10分钟验收。
- 观察器失败及修复记录（06:30 UTC）：首次运行`.runtime/e117-20261031/observe.py`因查询023字段但数据库仍为022而以`UndefinedColumnError`退出1；随后控制端5秒读取超时退出1。观察器已改为先探测列、缺失时报告补扫指标unknown，并把只读状态请求截止提高到15秒；06:29:46 UTC重跑退出0，控制状态响应正常。运行中仅新增了每来源哈希/状态和白名单错误类别输出，未触碰业务表。一次错误的运行手册路径检索也失败，正确文件为`docs/platform-v2/REMOTE_WORKER_DEPLOYMENT.md`。
- 冻结候选验证及阶段进度（06:33 UTC）：候选 `.2` 的policy、backend全量、browser、android作业全部success，完整Docker套件由tag触发的验证通过；发布作业`124466153517`仍在`Build clean-source development APKs`，尚未创建Release/镜像清单。进度台账提交`c5fe41b220d0226382a0f96a5ddc0dfa23fbcb27`已推送并由`git ls-remote`核对；不可变候选tag仍指向`65e62133ef98e48e81dace961a4c15568ebe9713`。
- 部署前租约及消息基线（06:33 UTC）：06:29–06:32只读快照退出0，6个来源租约均活动、检查点最新推进至06:30:48 UTC；Telegram connected/authorized，但仍degraded，状态为3个reconciliation_deferred、2个TimeoutError/unavailable、1个catching_up。该窗口数据库收到4条消息、0条带图；账户任务无queued/running/unknown，订单无pending/submitting/unknown。数据库仍022、补扫字段不可用。远端切换前需处理仍活动租约；不能把本基线计为部署后10分钟验收。
- 候选发布及部署资产核验（06:46 UTC）：发布作业`124466153517`完成，Android debug/unsigned APK构建、六个源码镜像推送、provenance校验及不可变预发行创建全部success。候选下载资产JSON解析通过，release manifest SHA-256为`81b5f34b0591ab002caf37657e4c3990f7ee5ca0edaa77f93b6c03445cf8df85`，Compose清单SHA-256为`62a02269aa3a63545820269cb84a156aef37a677bc9789103eea4acf079ad234`，policy SHA-256为`0c57524691297c6c0ad1eeac485598d12364588a5e1e3843da6a38f979941a46`。Docker内使用结构化manifest验证源码/tag、development与production_ready=false、Compose映射完全相等，Compose `config --quiet`退出0。候选API/Web/Collector/Executor/storage-tunnel镜像均实际存在且OCI revision及RepoDigest匹配tag `65e62133ef98e48e81dace961a4c15568ebe9713`；API/Web首次Compose拉取因继承`pull_policy=never`显示Skipped，改为按清单直接`docker pull`后均成功。远端按同摘要预拉Collector/Executor并验证revision，exit 0；远端候选env侧车目录0700/文件0600，真实Compose配置渲染退出0，仍未启动/替换服务。
- 远端源码与回退证据（06:46 UTC）：远端部署树不含`.git`元数据；旧桥文件SHA-256 `c0152e775a345c0aba043728119860e160f0c811d6bb518f269d599f6d9f33be`已只读保存至本地忽略目录并设0600，备份hash与远端一致。候选tag中的`infra/remote/api_bridge.py`与本地冻结文件SHA同为`1f0bbbbfca667edeeb8bd670c21091335b7b4d314c1fe04b27f21b4bf29e2800`。受保护远端旧env保留0600且8个所需键存在；旧worker仍运行，live-orders=false。探测期间Docker镜像检查的一条错误引号命令退出2，修正后各项成功；尝试对远端部署树运行Git状态因无`.git`退出128，改用文件hash备份方式，无远端文件被改动。首次远端Compose检查在10秒只返回hash，完整重试退出0。
- worker排空、迁移备份与候选恢复演练（06:54 UTC）：停止远端旧collector/executor退出0，均为exited；只读SQL确认来源租约从6/6 active自然到0/6，账户queued/running/unknown任务及pending/submitting/unknown订单均为空。重新导出归档使用固定PostgreSQL镜像`postgres@sha256:f4610795587492cd4fb887c2dc3de301b813278bb6f616992061141b5f15ac36`和仅含PG连接字段的0600环境文件；schema-only归档151,528字节，v2数据归档81,567,184字节，两个`pg_restore --list`分别338/61项，四份SHA均匹配。首个备份漏`--schema-only`，只读导出曾增长至456,077,358字节后以exit 137中止，作为`.partial.dump`保留且不用于恢复；修正后有效归档生成成功。
- 生产迁移前隔离恢复验证（06:54 UTC）：首次隔离恢复因默认`public` schema冲突退出1；第二次仍因脚本错误重建schema退出1；第三次遇到数据导入触发器依赖导致`v2_analysis_jobs`先于消息表触发退出1。只在任务专属无外网临时PG中修正为先删除空public schema、数据恢复用`pg_restore --disable-triggers`。一次候选迁移成功但版本断言误用整数23而退出1；修正为项目文件名迁移键后，`sh .runtime/e117-20261031/restore-and-test-023.sh`退出0，候选API真实执行`app.migrate`应用1条迁移，恢复后schema为`023_telegram_author_backfill.sql`，计数为6检查点/10映射/12,050专家消息/5待补扫；临时容器、网络和卷由脚本清理。生产库尚未写入。
- 部署门禁与生产迁移（07:25 UTC）：候选collector、executor、storage-tunnel三个镜像均有预期registry digest、image ID及OCI revision=`65e62133ef98e48e81dace961a4c15568ebe9713`；storage-tunnel首次手抄摘要末位错误导致`docker pull`退出1，按release manifest修正后本地镜像检查通过。远端HTTPS/CA探针首次退出1，根因是远端部署树没有`infra/config/storage_probe.py`；将无秘密模块传入独立只读探针目录（0444，SHA256与仓库一致）后，远端candidate collector容器以只读、禁写环境运行退出0：bucket认证成功、TLS验证开启、限定`v2/`列表为空、未执行写入。候选Compose API临时容器继承固定地址与当前API冲突、网络创建退出1且无残留容器；改在当前API命名空间运行受限SQL门禁退出0，结果schema为022、Leyu配置17、Telegram租约0/6 active、账户任务和未知/提交中订单为空、运行角色最小权限通过、API实盘硬开关false。远端bridge内经SSH反向socket执行只读`GET /internal/telegram/sources`返回200、6来源；本机remote-link报告unhealthy源于collector停机期间`/status`不通，不代表SSH child死亡或反向socket故障。切换manifest在Docker生成，`python -m infra.remote.deployment --manifest ...`退出0、eligible=true、13项观察无blocker。已执行`sh .runtime/e117-20261031/compose.sh run --rm --no-deps -T migrate`退出0，候选API迁移器报告applied_count=1；Docker内只读复验schema=`023_telegram_author_backfill.sql`、四个作者补扫列齐全、租约0/6 active、queued/retry/running审核任务为0。一次post-migration探针因shell引号错误在Python启动前退出2；改为容器stdin脚本后退出0。生产库中加密`auto_betting_enabled`保持true且未改；API/执行器实盘硬开关false。任务分支进度提交`b55b8fb`已推送。下一步原子替换远端只读桥接脚本并启动bridge/socket-guard，随后更新候选API/Web，再启动远端worker；所有worker仅在API路由、配置和硬开关复核后启动。
- 部署切换及真实 Telegram 观察（07:49 UTC）：候选bridge文件先校验远端旧SHA=`c0152e...`后原子替换为`1f0bbbbf...`，远端socket-guard/api-bridge先启动并使用候选collector摘要；反向socket只读`GET /internal/telegram/sources`返回HTTP200、6来源。storage-tunnel更新后healthy；候选API/Web/review-worker更新后均运行，API`/readyz=200`、Web healthcheck通过；实站Docker Chromium真实点击桌面1440与手机390的`all/live/scheduled/finished/cancelled`五个比赛阶段，按钮`aria-pressed`/active状态全部正确且无JS/5xx，当前公开赛事为空；`sh tests/browser/check.sh`重新运行31/31通过，包含原文图片、相册回复、普通成员搜索和状态切换竞态。候选远端collector/executor启动退出0，均restart=0、revision=`65e62133...`，executor `EXECUTOR_LIVE_ORDERS_ENABLED=false`；API最终门禁schema=`023_telegram_author_backfill.sql`、配置17、DB最小权限、账户任务/订单为空、API实盘false，业务加密`auto_betting_enabled=true`未改。真实观察起点`2026-10-31T07:33:57Z`，最终快照`07:48:21Z`，连续约14分24秒：connected/authorized均true，6/6来源租约有效，6个检查点均刷新，落库39条消息、6条含图；作者补扫从待处理推进到多项完成，窗口同一`content_hash`重复0。窗口媒体状态按最后10分钟只读统计为ready=11、pending=4、failed=3、unsupported=6；未把失败/不支持伪装成功。审核任务有9条历史回放自动抑制、2条无错误、最终另有queued=1/running=1，均非订单；账户任务和订单始终为空。Telegram最终状态为`state=connected`但`degraded=true`：3个reconciliation_deferred、2个unavailable（TimeoutError/other）、1个catching_up；控制端`last_success_at`存在，collector无重启，最近日志类别为HTTPStatusError/deadline/TimeoutError/batch_deadline/ConnectTimeout/history_deadline，指向上游同步/超时，不是候选进程崩溃；bridge BrokenPipe来自客户端超时，socket-guard无错误。全库重复平台ID是编辑历史多版本，窗口同版本重复0。该窗口证明多群租约、检查点、作者补扫和消息持久化运行；不证明Telegram上游所有来源已恢复无降级，外部状态需继续保留。
- 收尾健康复核（07:50 UTC）：remote-link在collector启动后恢复`healthy`且FailingStreak=0；API健康探针曾有一次3秒读取超时，容器healthcheck日志保留该失败，随后同一容器重试`/healthz` 76ms、`/readyz` 404ms均200，容器状态healthy，最近API日志只有对应短暂TimeoutError。未因此重启或放宽超时，作为瞬时负载证据保留。一次从Web容器执行Python探针因nginx镜像没有Python退出127，未发出请求；API容器内健康和就绪复验已通过。远端与本机候选服务未重启，分支`ce5d601bb83290eee2b7520d63ff544407b50bd1`已推送并与origin逐字一致。

### E116 — 本人乐鱼账号校验门禁与二次身份验证移除（2026-10-30，开发候选运行验证通过）

- 目标：让已登录用户可以配置、重绑并排队校验本人乐鱼账号；按用户要求移除全站 MFA、管理员备用码登录及会话内二次身份验证。常规密码登录、角色分离、会话有效期、CSRF、对象归属、限流与审计继续执行。
- 责任目录：`services/api/`、`web/app/`、`tests/`、`docs/`。前置依赖：现有乐鱼 App 登录签名/发现配置及本人账户异步校验任务；不触发真实下注。
- 当前证据：`PUT /api/v1/accounts/leyu` 及启用自动投注路径要求五分钟 `reauthenticated_at`，绑定拒绝发生在任务入队前；管理写入和本人导出也受同一近期验证门禁影响。`/root/GPT-team/example/registered_accounts.txt`（43,920条）与 `/root/GPT-team/accounts.txt` 均属于 ChatGPT 账号注册/账号池流程，不是乐鱼账号样本。
- 设计决策：不使用该批未知归属凭据对乐鱼发起试登录；只在受限 Docker 中读取脱敏格式统计，账号验证以本地协议替身和本人绑定数据库行为测试覆盖。删除 TOTP/备用码/MFA API 和页面、近期重验 API/拦截及相关重复密码挑战；保留正常主凭据登录、密码恢复、注销确认语、CSRF、角色与对象边界。遗留数据库列/表仅为兼容保留，不再由应用读取或写入，不执行破坏性数据清理。
- 实现动作：移除账号绑定/自动投注近期门禁；清理认证 API、生命周期路由、配置变量、前端登录和管理设置；增加“旧管理员因子已配置仍可仅凭密码登录”“已登录会话能绑定且任务入队”“MFA/重验接口已不存在”测试；替换旧浏览器 MFA 流程为登录、账户绑定和权限边界点击验收。
- 验收场景及失败处理：过期的历史 `reauthenticated_at` 不阻止本人绑定、普通用户不能绑定他人/管理员接口、无 CSRF 写请求仍拒绝、无资金 worker mock 校验能完成且不调用下注、管理员遗留 TOTP 密文不再触发挑战、网页无 MFA/再验证元素/API 请求。冻结后运行 Docker 身份/账户测试、完整后端回归及桌面/手机真实浏览器点击；失败保留证据，修复后重跑原检查。
- 阶段结果：认证 API 已删除 MFA、备用码与再验证路由及因子校验；网页和配置删除对应表单/入口；用户绑定、自动投注启用、导出和管理写入不再要求近期重验。会话主密码、180天有效期、角色、CSRF、对象归属、限流、审计、密码恢复及注销确认保留。数据库历史列/表未清理。
- 阶段验证：`git diff --check` 退出码 0；服务/API/网页活跃代码检索确认无 MFA 校验实现；隔离 Docker 解析了旧注册文件的记录数量及格式统计，未显示账号字段或用其访问外部站点。账户绑定以本地协议替身和临时 PostgreSQL 行为测试覆盖；无乐鱼真实账号登录或真实交易。
- 首次完整 Docker 回归：`sh infra/test/run.sh`，994项、401.696秒、退出码1；992项通过，2项失败均为旧测试仍期待近期二次验证（管理员审核重试403、本人的数据导出403）。已将预期改为旧时间戳不阻断操作，同时保留原因校验与真实过期会话拒绝；证据目录 `.runtime/test-evidence/1173441/`。
- 修正后完整 Docker 回归：`sh infra/test/run.sh`，994项、400.031秒、退出码0，零失败/错误/跳过；包含本人绑定、管理员登录、过期历史验证时间、CSRF/角色边界、会话180天失效及协议替身校验。证据目录 `.runtime/test-evidence/1189156/`。
- 浏览器 Mock 首轮：`sh tests/browser/check.sh` 退出码1；31个场景通过，注销流程1场景失败，旧脚本仍尝试填写已移除的当前密码字段；错误证据 `.runtime/browser-ui-1204631/results.json`。已删去注销与取消注销的旧密码输入步骤，保留确认短语和会话/CSRF检查。
- 修复后浏览器 Mock：`sh tests/browser/check.sh`，30个场景全部通过，退出码0；证据 `.runtime/browser-ui-1208025/results.json`。涵盖桌面/手机比赛状态、登录、本人绑定、管理员入口、注销及 Telegram 操作。
- 可访问性检查：`sh tests/browser/accessibility-check.sh`，桌面1440px与手机390px自动扫描/键盘场景全部通过，退出码0；零 Axe 确认违规，验证管理设置没有二次验证控件或 API 请求。证据 `.runtime/accessibility-ui-1210395/results.json`；不等同屏幕阅读器或真实设备验收。
- 模型配置 UI：`sh tests/browser/model-configuration-check.sh`，桌面1440px和手机390px候选、证据、审批、取消、激活及信任隔离均通过，退出码0。
- 真实 API 浏览器首轮：`sh tests/browser/integration-check.sh` 两轮中基础真实 API/临时PG场景与五身份矩阵通过；第二轮本人账户替身流程在注销 UI 旧脚本填写已移除密码字段时失败，退出码1，任务证据 `.runtime/web-integration-1212956/`。已删去这项旧输入步骤，下一步重跑同一双轮联调。
- 修复后真实 API 浏览器：`sh tests/browser/integration-check.sh` 退出0；第一轮真实 API+临时PG基础与五身份/角色矩阵全部通过，第二轮全部本人账户、管理员会员授权、账户健康核验、过期历史重验时间下配置成功、紧急停止与合成成交单回执场景通过。证据 `.runtime/web-integration-1219524/`；账户 HTTP 由明确标注 `synthetic_no_provider` 的替身处理，未访问乐鱼或发送订单。
- 当前部署基线：API/Web 均为 `e115-07e11a7`，端口3003；Docker 容器标签记录的 Compose 文件链、运行配置 `.runtime/runtime.env` 与 `.runtime/remote/local.env` 已确认。当前 `/healthz`、`/readyz` 均200。默认基础 Compose 指向 GHCR 基线，与本地候选栈不同；曾发生缺必需 `REMOTE_TUNNEL_IMAGE` 的无写入渲染失败（退出1）。复用精确文件链与 env 的 `docker compose ... config --quiet` 通过（退出0），镜像覆盖解析仍是当前 e115 基线。后续部署沿用该完整文件链及同一 env 文件。
- 最终差异审计：`git diff --check` 退出0；活跃 API/Web/Compose 配置中 TOTP/MFA/再验证代码检索无匹配（rg退出1）；变更敏感模式扫描0匹配（退出0）。保留已部署 Telegram QR 依赖 `qrcode.js`；Telegram 官方登录的二步密码与平台身份 MFA 分离。
- 实现提交：`d1961418853b3d6ebed7c428acaa1e96b22a0607`，`git commit`、`git push origin fix/TASK-78-hermes-telegram-sync` 与 `git ls-remote origin refs/heads/fix/TASK-78-hermes-telegram-sync` 均退出0，远端引用与本地提交一致。
- 进度提交：`5206063fd1ce20297e5fac00f9f07b7a4b57e136` 已提交并推送，远端分支引用与本地一致；本段运行证据随后单独提交。
- 候选镜像：按现有项目 Compose 文件链构建 API `ai-football-api:2.0.0-dev-e116-5206063`（ID `sha256:6efcbb515fc850d362019bd720905a20aa988bbd3024c492f41bb3f258959e00`）和 Web `ai-football-web:2.0.0-dev-e116-5206063`（ID `sha256:5441fa997d7e82b05ac87d167092a12e646fd5dea2f56683b87c367b7ccd9b0b`）；Docker 构建及 `up -d --no-deps --wait --wait-timeout 120 api web` 均退出0。只重建 API/Web，既有数据库、采集、执行器、Hermes、MCP、隧道与远端数据未改。此前 E114 候选镜像 ID 与 storage-tunnel 容器 ID 复核未变。
- 部署后服务核验：API/Web 均显示 healthy；API `/healthz`、`/readyz` 均200。退役路由 `/api/v1/admin/mfa/status`、`/api/v1/admin/reauthenticate`、`/api/v1/auth/reauthenticate` 均404。使用匿名浏览器访问会话状态端点返回401符合未登录行为。
- 部署后真实浏览器：在连接项目内部网络的 Docker Playwright/Chromium 中打开 `http://web`，实际载入50场赛事；桌面视口点击“管理入口”进入独立管理页，点击“管理员登录”并切换“用户登录”，两种表单均只有账号/邮箱与主密码，没有验证码、MFA或重新验证步骤。桌面截图 `.runtime/e116-account-auth-20261030/.playwright-cli/page-2026-10-30T05-22-51-949Z.png`；页面快照及控制台记录位于同目录。容器切换至390×844后确认 `innerWidth=390`、文档与body宽度均390，无横向溢出。控制台记录仅有匿名 `/auth/me` 与 `/admin/me` 的预期401；没有凭据输入或真实登录。Playwright 会话关闭成功；普通 `docker rm` 首次因浏览器容器仍运行退出1，随后对本次专用容器执行 `docker rm -f ai-football-e116-playwright` 退出0，临时容器已清理。
- 当前完成状态：E116 的隔离测试、真实 API 流程和本机开发候选部署后真实浏览器验证均通过。旧注册账号文件不是乐鱼账号列表，因此未将其凭据发送到乐鱼；真实乐鱼登录未执行，也没有真实投注。未声称远端生产发布或正式版本。

- 机械模板清单作废。节点数量由功能决定，研究、设计、实现及失败验收分别编写，不以数量代表质量。
- 根目录中文约束补齐目录职责、配置和密钥复用、Docker、权限、协议、工具、结算、订单、证据、Git 及发布要求。
- 规划阶段已提交推送，现进入业务开发。旧树删除与新实现按可审阅范围提交，每项通过后及时同步远端。
- 当前状态：e5cc40d干净提交Docker完整后端953项通过；API/审核工作器/Web候选2.0.0-dev.hermes.20261028.1已部署，源码摘要与该提交一致，真实HTTP双端14组点击通过。Hermes复用旧配置的真实文本、图片、多推荐、闲聊及过期盘口隔离场景通过；运行最近20分钟完成59次审核。TG四来源检查点均在5分钟内推进、7位启用专家昵称非ID占位，2来源受成员枚举权限限制。远端collector/executor各1、本机对应worker为0，API/executor实盘保持false；Android沿用771faf3的已验开发候选，本轮未改Android。早期AuthKeyDuplicatedError等失败仅保留为历史证据，不再作为当前授权状态。
- 审核、队列、结算、身份、Telegram、协议与续期代码已完成一轮统一隔离验证并推送；继续三端界面、个人账户、订单、恢复及发布，不在阶段汇报后等待再次要求继续。

## 已有证据及局限

| 编号 | 实际动作与结果 | 范围及局限 |
| --- | --- | --- |
| E01 | 外部归档 1,661,030,400 字节、58,281 项；SHA-256 为 `831a0ded11a10a0ae790895af05f9187d8e3325ced11ab6a7df6556b41215a8c`，比对退出码 0、权限 0600 | 前阶段记录；本轮再次从归档成功读取配置及协议，不等于旧服务完整恢复演练 |
| E02 | 旧 Compose 停止 web-console 与 inference，退出码 0；从主线 4a1c1cd 建立当前任务分支 | 前阶段操作；不再次清空工作区，不停其他项目，远端数据未删除 |
| E03 | Docker 浏览器取得桌面首页、390×844 手机首页及会员三张截图；研究报告生成退出码 0 | 完整页面族、交互与资源还未研究完 |
| E04 | 四类本地候选镜像构建、Docker 内语法及 Compose 配置检查退出码均为 0 | 只证明可构建及配置可解析，未证明业务和正式发布可用 |
| E05 | 临时 API 健康和就绪曾返回 200，另一启动进程发生并发 DDL 冲突 | 验证失败；迁移锁修复未重跑，没有迁移生产库 |
| E06 | Docker 只读检查归档中旧环境、部署及设置层，退出码 0；新环境共 24 个键、权限 0600，仅输出键名和存在状态 | 确认旧设置位于 PostgreSQL 的 runtime_settings；数据库和模型网关迁入但未验证生产有效；新 S3 配置为空不证明旧远端缺失 |
| E07 | 本轮 Docker 只读规划检查退出码 0；10 项规划回归测试全部通过、退出码 0；人工逐包复核研究、设计、实现及失败处理，独立 Hermes 上下文已改中文 | 当前 46 个工作包、332 个动作；无重复动作、长段模板、缺失字段、循环依赖或具体文件名；只验规划，不验实际业务 |
| E08 | 本轮同步远端 main，并核对本地及远端基线同为 4a1c1cd；git diff --check 退出码 0 | 当前任务分支不覆盖主线；最终提交范围及推送仍待核对 |
| E09 | 暂存内容检查首次退出码 2，发现计划末尾多余空行，已移除；九个规划路径敏感内容模式检查退出码 0，无匹配 | 提交前重跑暂存空白检查；模式扫描不是全面秘密审计 |
| E10 | 规划提交 d2f9b124e37e8b436f2f29cd36524d8d8d5fde00，提交、推送及 ls-remote 均退出码 0，远端引用与本地一致 | 仅九个规划与校验路径；未混入旧树业务删除。阶段工作已远端保存，系统业务仍未验收 |
| E11 | 新迁移入口的五项隔离数据库测试通过，退出码 0：双连接冷启动串行、重复升级、校验变化拒绝、失败批次完整回滚及只读角色版本验证 | 使用本任务新建 pgverify、tmpfs、内网无映射端口；首次旧测试库无密码连接失败及新库未就绪连接失败退出码 1 均已定位为测试前置，确认就绪后原测试通过，未接生产写入 |
| E12 | 四项配置行为测试通过，退出码 0：工作器不需要用户秘密、API 不需要模型密钥、非法密钥拒绝且不回显、旧引号及特殊字符正确编码 | 仅配置加载验证；容器编排的实际最小权限注入待整合 |
| E13 | 旧工作树删除独立提交 0aea167 并推送，命令均退出码 0，510 个已归档旧文件从新版本移除 | 没有再次删除工作区、远端库或归档；新同名忽略配置和正在开发模块保留 |
| E14 | 身份与会员冻结隔离测试 20 项通过、退出码 0，覆盖双会话、CSRF、TOTP 防重放、幂等授予、重叠权益、撤销及重绑停用 | 详见身份验收文档；HTTPS 客户端、密码恢复和敏感变更再次验证仍待完成 |
| E15 | 配置、CONNECT 白名单、SSH 主机校验及目录边界共 11 项通过、退出码 0；编排环境特殊字符实容器检查退出码 0 | 只使用项目内假环境，未再次访问外部配置；真实存储部署不等于通过 |
| E16 | 乐鱼协议、WS、会话、续期 API 及 Runtime 冻结测试 61 项通过，退出码 0；先前真实归档回放 1,868 场及 36 项报价退出码 0 | 代理仅登录单次请求；真实账户和新采集工作器整体仍未联调；未发现可核验官方赛果确认接口 |
| E17 | Telegram API 14 项与采集 18 项冻结测试通过，两命令退出码均 0 | 覆盖扫码模拟、检查点、媒体恢复、编辑删除、旧消息轮转、完整原文及分页；真实授权、图片清晰度和网页待验 |
| E18 | 统一冻结回归首次运行 160 项、9 跳过、1 导入错误，退出码 1；错误为统一验证器缺少 collector 包搜索路径 | 已定位为验证器路径配置，不能记为全绿；将补路径后原命令重跑。已执行的审核 10、队列 7、纯结算 9、结算 API 7 项通过；仍以完整重跑结果为阶段判据 |
| E19 | 统一冻结回归最终 202 项、零跳过，42.193 秒，退出码 0；API 开发镜像构建退出码 0 | 详细命令及历史失败见专家链路验收文档；只证明隔离场景，未证明真实提供方、上游采集、实盘或正式发布 |
| E20 | 编排阶段提交 3b0ae93，提交、推送及远端引用核对均退出码 0，完整远端号 3b0ae939e40c7365d7d96891b38f9fe8ca3a9b1e | 服务注入和目录边界已保存远端；业务阶段继续单独提交，不合入主线冒充正式版本 |
| E21 | 专家业务阶段提交 739a9f9，提交、推送及远端引用核对均退出码 0，完整号 739a9f9d4a5ab47a4851d83ef903bbad44448689 | 66 个业务、测试及证据路径已保存；未合入主线或发布正式版本 |
| E22 | 真实 Hermes v0.22.5 框架隔离联调通过、退出码 0：图片、中文项目技能、仅 6 项工具白名单、105 场赛事连续 6 页、精确盘口写入 1 项推荐、独立会话删除 200 | API 双工作进程与 MCP、PG 均为任务隔离容器；模型提供方是确定性替身，不证明真实视觉准确率或上游可用 |
| E23 | Hermes 联调前两次退出码 1：初始 MCP 延迟发现导致只读取技能；关闭延迟发现后测试提供方未解析框架不可信结果包装 | 已修复框架配置、只读技能工具集及替身解析，再执行原链路获得 E22；保留失败以区分配置缺陷和测试缺陷 |
| E24 | 配置、Hermes 健康与联赛查询 15 项定向测试通过，4.936 秒、退出0；九服务编排解析、边界及特殊字符检查退出0 | 新执行器凭据独立，API与执行器实盘双开关默认关闭；框架探测使用缓存且不需要提供方秘密，真实模型健康仍未验证 |
| E25 | 新统一入口从当前项目复制冻结源码，新建独立内网及临时PG，244项、零跳过，60.359秒、退出0；任务容器与网络按名清理 | 包含38项个人账户/订单协议与数据库用例；资金HTTP全部替身。报告和逐文件哈希保存在项目内忽略的运行证据目录，未混入真实数据 |
| E26 | 开发发布清单2项行为测试通过、0.078秒、退出0；精确原文报价证据9项回归通过、5.592秒、退出0 | 发布清单拒绝可变镜像、脏源码及APK与提交不一致；未运行远端发布，不证明仓库写权限或正式APK签名 |
| E27 | 个人账户与订单回归扩展40项，11.288秒、退出0 | 新增撤回推荐禁止预留；会员过期持久停用许可并取消未发送订单，续期会员不会自动重启。所有资金协议仍为替身 |
| E28 | 整合提交e3906d6，推送及远端引用核对退出0，远端完整号e3906d63fef8cb7496b2203ae62662be9c2feda0 | 本人账户、执行器、真实Hermes隔离链路、九服务编排及统一冻结测试入口已保存；网页和Android继续独立提交 |
| E29 | Android独立候选入口17测试、Lint、两种APK及签名元数据核验完整退出0，2分35秒；阶段提交58b9af3推送及引用核对退出0 | 远端58b9af3d88a80eb8208ef95b4cca142f59ca4446；设备安装成功，UI被模拟器SystemUI ANR阻断，截图保留，正式签名与升级未验收 |
| E30 | 恢复工具12项隔离测试退出0，6.634秒；37表前后金额与数量差异为空，媒体1项哈希一致；旧证据合成分类CLI退出0 | 精确命令及金额表见恢复验收文档；只证明构造样本与临时库的恢复，不证明外部旧档及生产恢复 |
| E31 | HMAC分页及公开台账12项通过，6.713秒、退出0；运维与只读订单最终4项通过，3.023秒、退出0 | 先前新增订单行测试缺少role前置退出1，补齐真实约束后原组合通过；无秘密白名单、查询范围绑定和依赖未验证状态已检查，非真实存储探测 |
| E32 | 开发验证与发布工作流用固定actionlint容器检查退出0；首次用框架系统Python解析YAML缺少模块退出1，随后改用专用检查器 | Actions源码引用通过ls-remote核对；尚未远端运行工作流或创建Release，不以静态检查代替发布成功 |
| E33 | 网页Mock22项和真实API+临时PG浏览器10项均退出0、浏览器错误0；网页阶段c4fc277提交及推送退出0 | 人工样本、无执行器或真实上游；S3原图503明确为未配置。参考布局仍有已记录主动差异；报告离线显示另行验证 |
| E34 | 新统一回归311项，113.178秒、退出1：跨用户绑定fixture缺少切换后session；续期fixture旧局部结构缺少新身份字段 | 两项均按当前生产约束补齐前置，原账户20项16.130秒及续期11项2.501秒退出0；完整组合仍待重跑，不把单项成功记为统一全绿 |
| E35 | 调研报告Docker断网浏览器1440及390宽、直接file读取均通过：22图完整解码、本地SVG可见、Mermaid源码可展开、零外部请求及JS错误，命令链退出0 | 编译时固定Mermaid11.4.1并保存锁和源图；报告不依赖外部CDN。网页22+10详细矩阵将未研究私有原站页、真实图片与配置ack单列，不能称全部像素复刻 |
| E36 | 原统一入口修复后完整重跑356项，141.343秒，零失败、错误及跳过，退出0；逐路径SHA-256与结果已保留 | 源父提交c4fc277，冻结时有未提交候选，committed_source_verified=false；不得冒称已提交源码的远端验收。覆盖恢复、质量、管理员重审、身份及分页，未调用真实提供方或资金 |
| E37 | 冻结网页最终24项Mock、11项真实API+临时PG、报告离线3场景均退出0；浏览器JS错误及报告外部请求均0 | 新增管理员换机、手机管理入口、单次备用码显示及恢复登录；真实S3、真实TG授权、配置应用ack与原站私有页面仍未验收，详见网页矩阵及哈希快照 |
| E38 | 离线审核质量20项行为测试0.803秒退出0；合成基线、候选及逐项差异三份报告生成退出0 | 6条合成消息、5项标注推荐，不是历史真实质量或盈利；候选图片执行失败单列，工具不自动激活模型新配置，详见质量验收文档 |
| E39 | Docker量级基线完整命令退出0，10k赛事、50k报价、10k消息/推荐、100作者；六项HTTP暖态p95为89.894/37.263/60.522/51.391/77.280/46.369ms | 合计0.75CPU、tmpfs临时PG、顺序小样本；90候选4页消歧99.716ms。索引只改善SQL而非HTTP尾延迟，未加迁移或中间件；不代表真实VPS磁盘、网络及百万记录性能 |
| E40 | 身份生命周期与MFA最终70项57.492秒退出0，审核管理17项10.023秒退出0；全部纳入E36 | 会话近期验证、双身份、恢复/注销保留对账、MFA原子换机及撤销并发均覆盖；管理重审保留历史且拒绝已接受信号重复任务。均为隔离账号，不表示生产管理员已迁移 |
| E41 | 身份、审核管理和有界运维阶段c9f84ba提交、推送及远端核对均退出0，完整号c9f84ba554ea579ebf770cfef7af2e79a5504cb7 | 29个明确路径；117个后端与验证路径对E36冻结哈希复核无变化，命令退出0。敏感模式扫描无匹配（rg退出1），不代替全面秘密审计；未合并主线 |
| E42 | 恢复、质量工具及量级测量阶段938f3a1提交、推送及远端核对均退出0，完整号938f3a16b61d93efd7b70d86bbe14ab0cde0886f | 29个明确路径，数据全部显式合成；原始计划与分位数保存，未触及外部旧记录或新增生产索引。GitHub Actions项目权限已只读确认启用，开发Release尚不存在 |
| E43 | 网页MFA及离线报告阶段2eb85bb提交、推送及远端核对均退出0，完整号2eb85bb92717a53c148a911803322762d17a3e9b | 34个明确路径；网页、图示及结果哈希封存，不将尚未发生的生产配置应用记为生效。开发工作流补显式候选标签触发后actionlint再次退出0 |
| E44 | 草稿审阅请求71已创建，当前范围、356项验证、迁移与回退说明已通过REST更新退出0 | 首次gh pr edit退出1为旧CLI访问已退役classic项目字段；改用明确项目REST端点成功。审阅入口https://github.com/1420970597/ai_football/pull/71；未合并主线 |
| E45 | CI阶段e5c13e4提交及远端引用核对退出0，完整号e5c13e412fbba760b5d9a050883e675d85bb8af0；GitHub两次触发均实际执行 | 远端浏览器及Android任务成功；backend统一测试后在Compose检查失败，不能记全绿。根因为root容器写0600假配置，runner普通用户不可读；将以调用者UID创建并保留0600，修复后重新验收 |
| E46 | 容器改为调用者UID后，完整Compose契约及特殊字符检查退出0；独立UID1001创建并读取0600假配置实际验证退出0；actionlint退出0 | Python检查及字符探针镜像固定摘要；push与PR并发组统一以分支名去重。修复后的真实GitHub普通用户任务仍须再次运行，不能用root本机成功替代 |
| E47 | 修复011433f提交、推送及远端引用核对退出0；GitHub运行40323622154后端、浏览器、Android均成功，整轮success | PR头011433febea15fe45d3b417f81e4706102205fcb；实际干净检出合并提交a0b188bbb35491e9203f00094d203fbd0985a9cb，后端356项、零失败错误跳过、100.992秒。下载原始证据退出0，不将PR合并快照冒称已合入主线或正式发布 |
| E48 | 个人日限额时区独立账户回归23项38.662秒退出0、零跳过，默认上海/UTC边界/柏林23和25小时自然日及跨日未知敞口通过 | 单独冻结前十一批与时区013，公平调度012开发中未混入；网页和完整迁移组合仍待验。冻结权限、旧临时库口令及新增断言表名三类前置失败已单独记录，未放宽业务校验 |
| E49 | 开发发布清单最终5项0.012秒退出0，镜像覆盖清单与基础Compose实际合并检查退出0；actionlint退出0 | 新增完整九服务不可变摘要覆盖，环境、挂载、网络及资源限制保持基础配置；使用显式假摘要验证映射，未拉取或启动候选镜像，尚未创建Release |
| E50 | Telegram采集35项0.811秒、API目录及来源25项19.629秒，均退出0、零跳过 | 普通群使用完整成员记录筛出真实管理员，目录完整分页及实体失败分类、来源独立人工信任、匿名身份不冒充个人已验证；历史真实群与头像未联调 |
| E51 | 公平调度64项116.259秒、复合外键加强后36项71.734秒、最终工作器25项47.902秒均退出0 | 跨工作器单专家预算与跨源轮转、锁竞争暂缓、旧迁移回填及41表恢复差异为空；均为独占资源受限临时PG。先前共享库清理权限错误及测试辅助同名错误保留在专项证据，不宣称生产吞吐 |
| E52 | 冻结网页38状态及29项专项检查、Mock25项、真实API隔离11项、断网报告3场景全部退出0；快照校验退出0 | 对比度、焦点循环/返回、手机操作目标、减少动画、每日时区和TG双搜索通过；25张报告图片解码正常。自动规则及CSS补测不等于完整WCAG或真实屏幕阅读器验收，失败基线与修复尝试均保留 |
| E53 | 本轮统一组合407项171.734秒，退出1，406通过、1失败、零错误跳过 | 唯一失败是统一入口全局禁用日志导致锁竞争告警的assertLogs捕获不到；锁定任务状态行为通过。将入口改为根空处理器静默，保留局部日志捕获；不放宽业务断言，原组合修复后重跑 |
| E54 | 修复后的统一组合407项169.557秒，退出0，零失败、错误和跳过；独立源码快照证据保存于项目运行证据目录 | 覆盖所有当前迁移（含012审核公平和013限额时区）、身份、Telegram、Hermes、恢复、账户、浏览器契约及发布清单；全部是隔离替身或合成数据，不证明真实上游、生产存储或资金操作 |
| E55 | 任务规划检查在补充稳定记忆证据后通过，46个工作包、332个动作、166项有证据完成，退出0；差异检查退出0 | 规划检查只验证结构、证据引用和重复/依赖规则，不把未验收的真实上游、远端部署或设备UI标记完成 |
| E56 | 提交2550e10对应GitHub CI后端失败、浏览器和Android成功；后端退出2原因为首次拉取PostgreSQL镜像后Unix socket初始化竞态，日志记录连接失败 | 已在900aaab将就绪探针及建库命令统一改为127.0.0.1 TCP；本地脚本仍使用隔离资源，待GitHub重跑确认 |
| E57 | 提交03d4df1对应GitHub运行40330108876后端、浏览器、Android全部成功；三项作业均退出0 | 后端远端重跑确认TCP就绪修复，浏览器与Android继续通过；开发候选尚未发布，真实提供方、远端部署、生产签名和设备验收仍未完成 |
| E58 | 草稿PR71正文已更新统一回归407项及CI运行40330108876，退出0；候选标签尚未创建 | 发布前范围、已知限制和回退说明已写入项目内文档；下一步创建不可移动开发候选标签并以流水线实际结果为准 |
| E59 | `v2.0.0-dev`候选流水线40331095572验证及发布四个作业全部成功；Release资产下载、清单源码提交/6镜像/9服务映射/APK哈希校验均退出0；6镜像按摘要拉取成功 | Release为非生产候选；Web发布镜像在项目内临时内部网络+API无状态桩下healthz、首页和静态脚本冒烟通过。无依赖单独启动因Nginx找不到api的预期失败已保留，临时资源已清理。未启动真实数据库、S3、Telegram、Hermes或资金链路 |
| E60 | 只读盘点项目外已整理来源：配置目录、历史备份目录和文件权限/字段形状核对退出0；无外部写入、移动、删除 | 已记录旧数据库/S3/SSH/Telegram/乐鱼/模型来源及目标服务边界；秘密值未输出，旧模型与历史数据库未导入。真实连接、迁移、采集和授权仍未验 |
| E61 | 规划检查在新增只读来源证据后通过，46个工作包、332个动作、178项有证据完成，退出0；差异检查退出0 | 只读盘点不等于配置有效、真实授权或生产部署；未将外部秘密复制入项目 |
| E62 | 外部配置只读契约检查在隔离Docker中通过：14个来源文件、13个JSON、18个必需环境键，所有文件权限0600；退出0 | 仅验证格式、字段存在和权限，未回显值、未解密会话、未连接上游、未修改外部目录 |
| E63 | 提交bb16295对应GitHub运行40337262459后端、浏览器、Android全部成功；三项作业均退出0 | 只读来源台账修正后仍通过；开发候选Release继续固定37c511d，不将后续文档提交混入已发布资产 |
| E65 | 审核故障分类新增单元2项退出0；统一隔离回归410项173.823秒退出0，零失败、错误和跳过，超时用例验证失败轨迹 | 新增分类仅保存类别/重试性/尝试及耗时，保留原异常类名兼容；框架逐工具耗时、动态内部回合预算和真实上游故障注入仍待验 |
| E64 | 订单限额与未知回执扩展统一回归408项172.672秒，退出0，零失败、错误和跳过；6路不同推荐并发最多2条预留，紧急停止清零敞口 | 覆盖未知发送、执行器取消、过期租约、错误/准确/重复回执及重复点击；全部使用隔离PG与Fake HTTP，不证明真实资金接口或生产容量 |
| E66 | 共享冻结代码统一回归411项175.722秒，退出0，零失败、错误和跳过；覆盖审核故障分类、订单并发、恢复、Telegram目录、时区及发布校验 | 仅使用隔离Docker、临时PG、Fake HTTP和合成数据；模型429、MCP、订单回执有替身覆盖，真实数据库/S3/TG/乐鱼/模型及资金接口未调用；逐工具耗时和图片字节仍待框架协议支持。证据快照保存在项目内运行证据目录 |
| E67 | 提交4b437a8对应GitHub运行40342241144后端、浏览器、Android全部成功；三项作业均退出0 | 共享冻结的审核分类和订单限额改动通过远端验证；开发候选Release仍固定37c511d，真实提供方和生产部署保持未验 |
| E68 | 提交7923141对应GitHub运行40343803687首次 Android 构建在 Google SDK 下载遇到 HTTP/2 `INTERNAL_ERROR`，退出1；仅重跑失败作业后 backend、browser、android 全部 success，退出均为0 | 错误发生于工具链传输且重跑消失，未改构建源码；仅证明当前提交远端 CI 通过，开发候选Release仍固定37c511d，外部服务和设备验收未完成 |
| E69 | `sh infra/test/run.sh` 对冻结工作区执行414项，173.451秒，退出0，零失败、错误和跳过；新增撤销会话暂停重试、配置替换恢复、在线撤销断连、二维码会话跨采集器重启续接及远端账号摘要读取 | 证据位于`.runtime/test-evidence/3870038/`；仅为隔离数据库与假 Telegram 客户端，真实数据中心迁移、掉线、账号限制和 Telegram 登录仍未验；修改代码后的远端 CI 待提交后运行 |
| E70 | 提交419c0e1对应GitHub运行40348834604后端、浏览器、Android全部成功；三项作业均退出0，远端引用与本地一致 | Telegram会话撤销及重启恢复改动完成远端验证；开发候选Release仍固定37c511d，真实Telegram/S3/乐鱼/模型和设备验收未完成 |
| E71 | 提交defc97a后，Android 候选入口在 Docker 内完成 Gradle、Lint、双 APK、apksigner 和 aapt 校验，退出0；父提交与 Android 测试子树均干净，发布清单新增 release 包名/版本码/版本名校验、网络权限白名单和危险权限拒绝，6 项清单测试及实际构建元数据策略检查退出0；APK SHA-256 为 debug `b58477e90e72fcd0fc78c57392552a526d145c7b91d442ec6897498fef0c6b0b`、unsigned `b7e9b446432b268a4a174281a1edaea7aac0c7c62152faddd8ac03ff5d1978db` | 当前 APK 为开发 Debug 签名和未签名变体；正式密钥、升级连续性、设备 UI 仍待验收；候选镜像摘要文件使用发布流水线生成，当前本地 images.json 仍是构建输入模板，未冒称正式 Release |
| E72 | 用户授权读取项目外备份后，在项目内 `.runtime/` 生成 0600 运行环境、隧道和乐鱼签名/发现引用；从备份恢复 S3 CA 并将证书权限调整为 0644；Compose 配置解析、全栈启动、Web healthz、PostgreSQL 远端隧道和 S3 `head/put/get/hash/delete` 探针分别退出 0；旧乐鱼设置 16 项加密写入当前台账并重启采集器 | 采集器已加载旧配置并通过 WebSocket 收到事件，但轮询上游当前返回 `upstream_rate_limited`；Telegram 未授权；未启用真实投注。运行时目录与密钥未纳入 Git，S3 探针只删除自身测试对象 |
| E73 | 提交 4415022 的 Telegram 媒体证据、对象前缀/用途留存元数据、稳定管理员身份及跨群变更测试完成；collector+directory Docker 冻结测试 41/41、Hermes 图片校验 4/4、管理脚本语法检查均退出 0 | 真实 Telegram 头像下载、S3 媒体上传/过期链接和对象生命周期仍未联调 |
| E74 | 提交 99a141b 的异常日志脱敏、Android 默认地址扫描和手机会员中文/完整日期断言完成；API 错误日志 2/2、Android 构建/Lint/双 APK/元数据校验、脚本语法及 diff 检查均退出 0 | 真实设备、正式签名和完整浏览器流程仍待验；GitHub 运行 40360501261/40360516968 当时尚未完成，不能提前称远端全绿 |
| E75 | 提交 e844dfc 修复移动端会员验收的异步导航竞态；浏览器 29 场景全部通过，Docker 冻结回归 426 项通过/204 项隔离跳过，规划检查、Compose 解析、Python 编译、只读归档审计和 S3 精确对象探针均退出 0；GitHub Platform verification 40363635100 全部成功 | 远程 issue #72 已记录上游 `upstream_rate_limited`、Telegram 真实授权、正式设备和配置夹具剩余缺口；真实投注保持关闭 |
| E76 | 提交 299108c、3ca1ef2、9f511d3、2e5b93e 后，冻结工作区 Docker 回归 437 项，181.797 秒，零失败、错误和跳过，退出 0；Compose 配置解析、差异检查和规划检查均退出 0。采集器重建后连续运行超过 10 分钟，控制状态可读，WebSocket 403 进入 `backoff`，不再触发容器重启；管理员诊断增加采集器、Telegram、乐鱼和 WebSocket 分项状态，媒体孤儿仅在删除成功后确认 | 上游当前仍返回 `renewal_api_rejected`，WebSocket 仍为 `websocket_http_403`，Telegram 尚未真实授权；这轮证明故障隔离、退避和状态诚实性，不证明真实数据源已恢复。自动投注继续关闭，真实 S3、Telegram、乐鱼和 Hermes 仍待授权联调 |
| E77 | 提交 3fa4a59、586719a 后冻结 Docker 回归 440 项，177.850 秒，零失败、错误和跳过，退出 0；S3 临时对象写入/读取/哈希/删除实容器探针返回 `healthy`，MCP 流式 HTTP 探针返回 `reachable`，API 重建后 Compose API 与 review-worker 均 healthy；规划检查和差异检查退出 0，P41-01 已勾选 | S3 探针只使用并删除自身 `health/` 对象，未触碰业务对象；MCP 探针只建立并关闭响应流。乐鱼上游拒绝、Telegram 未授权、Hermes 真实模型语义和正式设备仍未验收 |
| E78 | 提交 4ed560b 后冻结 Docker 回归重跑 440 项，182.506 秒，零失败、错误和跳过，退出 0；管理诊断新增 Telegram 检查点时间与最大消息高水位，规划检查和差异检查退出 0，P41-02 已勾选 | 首次同轮回归曾因临时数据库 teardown 的 `pg_signal_backend` 权限竞态退出 1，未被计为通过；重跑在相同代码上全绿。该证据不代表真实 Telegram 已授权或消息高水位已推进 |
| E79 | 提交 e7f77f3 对应 GitHub Platform verification `40427860508` 的 backend、browser、android 三项作业全部 success；远端分支引用与本地一致，Compose 解析退出 0 | 远端 CI 仍只使用隔离 PostgreSQL、假配置、浏览器夹具和开发 Android 构建；不替代真实乐鱼/Telegram/Hermes、S3 生产数据和正式签名验收 |
| E80 | 提交 fc2be0e 对应 GitHub Platform verification `40428788326` 的 backend、browser、android 三项作业全部 success，退出均为 0；backend 冻结测试、Compose 契约、浏览器 API/响应式场景和 Android 构建/Lint 均通过 | 仅为项目验证流水线；Node.js action deprecation 为 GitHub 注记，不影响本轮作业结果。真实上游授权、生产数据、正式签名和设备仍未验收 |
| E81 | 提交 3a6e820 对应 GitHub Platform verification `40429560428` 的 backend、browser、android 三项作业全部 success，退出均为 0；远端分支仍与本地一致 | 本次仅更新进度文档；远端验证仍不覆盖真实乐鱼/Telegram/Hermes、生产 S3 数据、正式签名或真机 |
| E82 | 提交 d2c55ff 对应 GitHub Platform verification `40430406945` 的 backend、browser、android 三项作业全部 success，退出均为 0；远端分支引用核对通过 | 该轮仍是隔离验证流水线；真实授权、生产数据、正式签名与设备验收保持未完成 |
| E83 | 管理员可选 TOTP、账号标识和前端变更在 Docker 冻结回归 442 项通过、180.607 秒、退出码 0；浏览器隔离 25 场景及真实 API+临时 PostgreSQL 11 流程均通过；API、审核工作器和 Web 镜像构建及定向重建成功。现有管理员记录事务更新后，运行 API 的管理员登录、身份读取、密码单独重新验证均返回 200；运行时配置权限为 0600 | 首次回归曾因新增 reauthentication 测试未带管理 CSRF 退出 1，修正测试后全量重跑通过；测试不覆盖外部身份服务。运行账户为受保护环境配置，凭据不写入 Git/README |
| E84 | v1 Telegram PostgreSQL 通过归档固定主机密钥的 SSH 隧道以只读事务查询成功：应用 ID/Hash 已配置、来源 3 条、DB 配置行无会话；授权的受保护配置内另有可由 Telethon 离线解析的 StringSession 与 3 条映射，验证期间禁网、未连接 Telegram | 旧 Fernet vault 密文与其 key 不匹配，不能从该文件恢复会话；尚未写 v2、尚未连接 Telegram，当前正在核对三方映射并准备加密迁入 |
| E85 | v1 乐鱼 Android 登录、动态域名、token/requestId 边界与 v1 H5 赛事/盘口/赛况链路整理为 [REA 分析文档](V1_LEYU_IMPLEMENTATION_ANALYSIS.md)，保存当前 REA 证据包于被忽略的 0600 运行目录 | 原始 APK 是加固壳；细节引用 v1 归档中的校验 DEX 静态证据及旧 H5 抓包。没有运行目标 APK，不证明当前上游端点仍有效 |
| E86 | v1 配置只读核验后，API ID/Hash、StringSession、3 个来源、7 个作者映射已加密/事务化迁入 v2；v2 数据库键/行数复核一致，v1 加密 session 与导入 session 匹配。v1 运行设置显示 Telegram 开启且当前断连；v2 实连出现 `AuthKeyDuplicatedError`。collector 对初始连接和运行中来源请求增加失效指纹闸门；Telegram 专项 26 项及 Docker 全量回归 444 项分别通过，后者 187.228 秒、退出码 0；最终 collector 镜像构建/重建退出码 0，等待 22 秒后的状态仍为 `session_invalid`、未授权、无来源同步，3003 `/healthz` 返回 200 | Telegram 官方定义为授权会话在并行 TCP 连接中重复使用，服务端会废弃该密钥并要求重新登录；具体哪一个并发连接触发无法从只读状态确定。设置页需要一次扫码完成新授权；来源访问和真实消息入库尚未验收 |
| E87 | 2026-10-27 对乐鱼续期台账复核发现账户处于 `unknown`、租约已过期，最近一次任务为 `expired_attempt` 且已提交登录；将用户先前提供的按量代理提取地址写入 v2 加密配置，并经管理员重授权解除过期保护。随后真实重试结束为 `app_transport_failure`，元数据仍为 `login_submitted=true`，账户再次进入 `unknown`；App token 更新时间未变化。采集状态为 `unavailable`，WebSocket 为 `websocket_http_403`，重连 135 次且收到 0 条 | 真实登录请求提交后连接层未收到可确认响应，不能判断上游是否处理该请求；为避免重复认证，续期保护保持锁定。乐鱼赛事采集没有恢复，普通轮询及比赛数据仍未验收；还需核对动态域名和代理链路并解决上游传输/WebSocket 403。未经核验不再次提交登录 |
| E88 | 将续期传输失败从笼统错误拆为提取/登录作用域的 HTTP 状态、代理连接、连接超时、读取超时及读取失败类别；只记录脱敏类别和数字状态码。乐鱼续期/协议 Docker 定向回归 45 项通过，0.168 秒、退出码 0；宿主机 `py_compile` 与 `git diff --check` 退出码 0 | 新错误分类尚未部署到运行采集器，也尚未执行针对新分类的真实登录；须先构建并确认配置仍指向用户指定提取服务，再由单次续期结果判定代理 CONNECT 与 App 响应情况。真实业务仍未恢复 |
| E89 | 新版分类器运行后，管理员解除旧 `unknown` 并只触发一轮代理续期；远端尝试 `2026-10-27 06:11:49Z` 至 `06:12:00Z` 的结果为 `proxy_login_connect_failure`、`login_submitted=true`，无数字拒绝码，token 未更新，账户重新进入 `unknown`。这一轮未向代理发独立出口探测请求。46 项 Docker 定向回归通过，0.192 秒、退出码 0；`py_compile` 和 diff 检查退出码 0 | 当前证据指向代理 CONNECT/网络建连阶段，无法仅凭通用 ConnectError 断定 DNS、拒绝、不可达或目标连接原因；因而增加仅使用系统错误码映射的细分，未记录主机、异常原文或密钥。采集仍未恢复，细分版尚未部署 |
| E90 | 对照青果官方按量提取说明，确认 `server` 是代理地址、`proxy_ip` 是真实出口 IP，且代理授权必须使用 IP 白名单或独立账密；旧配置只含 IPRoyal 账密。新增青果 Authkey/Authpwd 管理字段，沿管理员加密配置、采集器远端读取、个人账户执行配置透传，仅供续期登录代理；缺失时不提取、不发送登录。47 项乐鱼续期/协议 Docker 定向回归通过，0.179 秒、退出码 0；相关 Python 字节码及 diff 检查退出码 0 | 账密未在旧配置中找到，尚未写入新设置；运行采集器仍是上次细分版且续期账户处于 `unknown`。尚无登录成功、token 刷新、场馆启动、赛事轮询或 WebSocket 恢复证据。需在管理员设置写入青果代理账密，再做一次授权无资金联调 |
| E91 | 冻结源码 Docker 隔离回归 `sh infra/test/run.sh` 完成 452 项，178.702 秒，零失败/错误/跳过，退出码 0；包含临时 PostgreSQL、管理员密文设置与脱敏、个人账户提取代理账密透传和续期范围；`py_compile`、`git diff --check` 退出码 0 | 文档指定的 `scripts/cpu-limited.sh` 在当前仓库不存在（尝试退出 127）；改用仓库实际隔离入口，其数据库与测试容器均设了 CPU/内存限额。当前测试不能证明青果真实认证成功；API、Web、Collector 尚未重建部署 |
| E94 | 选择性盘口预算、旧报价保留、队列满重同步、同一快照去重、配置键白名单及 key-only 青果提取校验完成；定向 Docker 回归 129 项，通过 0 失败/错误/非预期跳过（19 项环境条件跳过），退出码 0；`infra/check-runtime.sh` Compose 边界检查通过，`py_compile`、Node 语法和 `git diff --check` 退出码 0。提交 `ee58839` 冻结源码的 `sh infra/test/run.sh` 全量回归为 468 项，178.223 秒，零失败/错误/跳过，退出码 0；证据快照 `.runtime/test-evidence/1619626` | 仅为隔离验证；青果真实提取、乐鱼登录/动态域名、WebSocket 和生产赛事仍未恢复。key-only 逻辑依据青果公开 API 文档，未在本轮消耗真实代理额度 |

没有记录过的命令不补造退出码。原阶段证据和本轮新增结果分开标注。

## 本轮验证命令

下列检查以只读挂载、禁网和资源限额在容器内执行，不安装宿主依赖：

```sh
docker run --rm --cpus=0.5 --memory=256m --network=none \
  -e PYTHONDONTWRITEBYTECODE=1 -v /root/ai_football:/w:ro -w /w \
  python:3.12-slim python -m unittest discover -s tests -p test_planning.py -v

docker run --rm --cpus=0.5 --memory=256m --network=none \
  -e PYTHONDONTWRITEBYTECODE=1 -v /root/ai_football:/w:ro -w /w \
  python:3.12-slim python tasks/check_planning.py

git diff --check
```

首次及最终提交前运行结果：两条 Docker 命令退出码均为 0，10 项测试通过；规划检查为 46 包、332 动作，无重复和依赖错误。暂存空白修复后重查退出码 0。进度回写后的完成项继续检查证据引用；检查器只读，语义研究与业务验收仍须单独核验。

### E92 — LeYu 状态冲突诊断

- 运行时只读诊断确认：采集器可读取远端 LeYu 配置（HTTP 200，revision 3），最近续期请求在数据库侧以 `unknown` 结束，原因分类为 `proxy_login_connect_failure`，且 `login_submitted=true`。因此系统按安全规则拒绝新的续期领取，未再次发送登录请求。
- 原采集器把所有续期 API 非 2xx 统一映射为 `renewal_api_rejected`，无法区分 unknown 状态冲突。现按 409、401/403、422、5xx 分类为状态冲突、鉴权失败、请求无效和服务端不可用；新增脱敏回归测试。
- 状态映射隔离测试 9 项通过，退出码 0；改动文件 `py_compile`、`git diff --check` 退出码 0；采集镜像构建及容器替换退出码 0。运行时只读诊断现已显示 `renewal_state_conflict`，对应当前 unknown 续期记录。
- 外部续期仍未恢复：该记录最近结果为代理登录连接失败，且旧版本错误读取了残留青果提取字段。后续只允许使用历史 IPRoyal 登录代理配置；在新实现部署并完成一次受控验证前，不重复发送登录请求。
- 管理设置 API 不再暴露青果提取地址或 Authkey/Authpwd 字段；IPRoyal 登录代理 URL 继续按密文保存并以 `configured/masked` 形式回显，隔离身份 API 回归覆盖不回显要求。
- 管理诊断回传采集器已加载的 LeYu 配置版本，页面展示“采集器配置版本”或“未回执”，不再把数据库持久化误称为运行时热生效。
- 设置 API 把数据库期望版本与采集器回执版本比较，明确区分已应用、等待读取和采集器不可达；采集器在读取配置后即使上游失败也回报当前版本。隔离身份测试覆盖 applied 与 renewal 冲突原因并存的状态。

### E93 — 实时采集指标与配置原子切换

- WebSocket 实时采集增加固定 256 样本的内存窗口，分别统计源时间到接收、队列等待、源时间到 API 写入完成的 P50/P95；同时回报队列深度/高水位、事件接收/写入、丢弃、失败、重连和最近事件时间。无效或未来源时间不进入延迟样本，不存储推送原文或新增本机历史。
- 管理诊断采用字段白名单转发标量，并在设置页区分当前来源状态、期望/已应用版本和最近配置校验时间。格式无效的新版本保留上次已校验配置并继续使用旧会话采集；远端配置 API 不可达或授权失败仍按错误退避，不以缓存掩盖。
- Web 入口增加 1 MiB 请求体上限、30 请求/秒的客户端地址限流（突发 60），并拒绝 `/internal/`、`/mcp/`、`/hermes/` 入口。新增浏览器集成场景核验内部路由与大请求拒绝。
- 定向 Docker 回归覆盖 LeYu 协议、实时队列/延迟、运行配置保留和管理员诊断白名单：51 项通过，退出码 0。改动文件 `py_compile`、前端 `node --check`、`git diff --check` 退出码 0。
- 上一冻结版本的实时指标全量回归为 455 项通过，退出码 0；其后添加的配置原子切换和 Nginx 入口变更尚待本轮冻结全量回归与真实 Nginx 浏览器集成验证。运行容器尚未换成此源码，因此不声称运行中的采集器已具备这些新指标。
- GitHub 仓库当前未配置 Release 签名 Secret、稳定发布变量或受保护发布环境；远端当前只有 `v2.0.0-dev` 预发布。正式签名、主线合入、真实授权采集和生产域名/HTTPS仍是稳定发布门槛。

### E94 — 选择性盘口预算与边界修复

- 活跃、临近开赛和远期赛程使用独立刷新预算；完整赛程仍每轮拉取，但盘口请求只发给到期场次。成功上传后才推进刷新水位，部分刷新不会关闭历史盘口；定向用例覆盖预算边界、缺失盘口容器、回填旧报价和上传过滤。
- 实时队列在满载时计数并触发全量重同步，同一消息快照去重但保留独立比分/盘口更新；诊断只展示订阅请求数，不把本地发送当作上游确认或漏订阅率。
- 续期代理校验仅接受 IPRoyal 登录代理 URL 与可选密码模板；不再执行青果提取、Authkey/Authpwd 透传或 `proxy_ip` 依赖。
- 定向 Docker 用例 129 项通过，19 项按环境跳过，退出码 0；Compose 边界检查、Python/JavaScript 语法和差异检查均退出码 0。

### E95 — 动态域名与代理连接状态诊断

- 当前采集容器直连公网出口只读测得为 `152.53.126.151`；未通过代理访问目标。动态发现配置在容器内返回 1 个候选域名，使用现有 App 签名配置对候选执行无口令 `preInfo` 验证成功；候选域名已通过管理设置热更新到乐鱼配置版本 4，采集器回执版本 4。
- 在不发送普通采集请求、不启用投注的前提下执行两轮有界续期重试：一轮返回代理 CONNECT HTTP `408`，另一轮及后续不同短效出口为 `proxy_login_connect_failure`；未收到 App 登录成功响应，token 未更新，盘口/比赛仍无新鲜数据。WebSocket 当前为 `websocket_http_403`，收到事件数为 0。
- 代理诊断修复将 httpcore CONNECT 的数字状态码以脱敏 `last_error_code` 回传；PR #73 backend/browser/android 全部成功后已合入 `main`（合并提交 `f88defb`）。当前失败证据指向短效代理到乐鱼 App 的 CONNECT/网络路径，不能据此声称代理质量、账号密码或乐鱼账号任一方单独确定有错。
- 青果公开说明要求短效代理首次提取前加入发起连接服务器公网 IP 白名单；若使用账密则需该业务 Authkey/Authpwd。项目代码同时支持 key URL 鉴权、完整账密和白名单模式，拒绝半套账密；本轮没有将代理用于普通采集，也未额外消耗探针请求。

### E96 — IPRoyal 登录代理路径纠正

- 复核历史配置和运行代码确认：v1 的有效登录代理是 IPRoyal，旧实现因 `leyu_login_proxy_extract_url` 优先级错误才实际尝试青果；此前不能把失败归因到 IPRoyal 代理本身。
- 移除青果提取地址、Authkey/Authpwd、`proxy_ip` 和独立出口地址的配置透传；管理员设置和个人账户协议白名单只保留 IPRoyal 登录代理 URL 与密码模板。
- IPRoyal 代理仅在 App 登录时建立一次 59 秒粘性会话；登录客户端先经同一代理访问 `https://ipv4.icanhazip.com/`，校验公网 IP 后将该 IP 写入签名头并提交一次登录。preInfo、场馆启动、余额、订单和 WebSocket 均不使用该代理。
- IPRoyal 代理单元和签名登录轨迹回归 24 项通过；冻结 Docker 全量回归 468 项通过（0 失败/错误/跳过，退出码 0），API、Collector、Executor、Web 已重建替换。受控运行验证记录 `renewal_finished / success / login_submitted=true` 并写入新 App token；随后上游返回 `upstream_rate_limited (0401038)`，因此只证明 IPRoyal 登录链路已生效，不把限流状态标为实时赛事恢复。

## 配置复用与真实缺口

详见 [脱敏配置复用台账](CONFIG_REUSE.md)。用户已明确允许只读访问项目外配置并授权读取备份；本轮仅只读 v1 配置源、以 StrictHostKeyChecking 隧道查询旧设置。向当前 v2 项目内数据库导入属于用户明确要求的配置复用，不会向外部目录写入。新生成密钥不能替代旧会话解密主密钥。

| 链路 | 当前事实 | 下一验收 |
| --- | --- | --- |
| 身份会员 | 70项身份/MFA及独立权益场景隔离通过，恢复、注销和管理员原子换机已实现 | 生产引导与客户端真实 HTTPS/设备集成 |
| 数据迁移 | 独立迁移锁、只读校验及合成数据加密恢复通过 | 外部旧数据升级、生产最小权限角色及远端恢复 |
| 乐鱼 | 已修复层级、日期、盘口线、源时间和续期，样本及模拟行为通过 | 真实上游联调与官方赛果证据仍缺 |
| Telegram | 检查点、媒体恢复、编辑删除及完整原文已隔离验证 | 真实扫码及消息、媒体端到端 |
| Hermes/MCP | 实框架连续 105 场检索、图片、技能及盘口写入通过 | 真实模型语义准确率及上游样本 |
| 结算统计 | 十进制分盘、期间、比分基线及结果修订账本已通过 | 官方赛果输入与历史证据迁移 |
| 网页 Android | 网页24 Mock及11真实API隔离场景通过；Android17项、Lint和两种APK编译通过 | 真实存储图片、Android设备UI/配对/升级与正式签名 |
| 账户订单 | 本人异步校验、独立执行器、撤回与会员过期持久停用，已纳入356项统一回归 | 真实账户授权、查单与官方结算协议仍待验收 |
| 生产发布 | 未切换、无正式 Release 及远端资产 | 真实联调、恢复及资产核对 |

## Git 阶段记录

- 分支：`feat/TASK-70-platform-v2`；基线：`4a1c1cd68db6f2b12f8c836edc126b33de8c4b97`。
- 已归档旧树删除提交 0aea167；不再重复清空。后续按已冻结并验收范围显式暂存。
- 规划阶段提交 d2f9b12，已推送至 origin/feat/TASK-70-platform-v2，远端完整提交号与本地相同，见 E10。
- 本次回写把规划提交与远端核对动作标为有证据完成；后续业务、最终审阅、镜像和 APK 发布保持未完成。
- 后续业务与清理分别补验证、迁移说明和审阅，不以规划检查通过判定完成。

## 下一步

1. 将本轮统一回归、公平调度、Telegram、时区和网页冻结范围分别提交推送并核对远端引用。
2. 完成新提交CI与开发候选资产流水线验证；不以开发候选代替正式发布或生产可用。
3. 按当前验收矩阵更新任务；外部旧配置、真实提供方、远端部署及设备UI保持待验收。
4. 每个阶段即时更新状态和Git证据；用户目录边界持续生效。
5. 远程问题跟踪：[#72 候选栈恢复后的上游限流与真实联调待办](https://github.com/1420970597/ai_football/issues/72)。下一轮计划见项目根目录的下一轮验证与优化计划。

### E97 — 当前官方 APK 核验与有预算的采集恢复（进行中，2026-10-27 UTC）

- 当前官方版本接口返回 2.0.2 / 202，已下载主包及修复包，核对官方 MD5、三代包相同签名及 REA 静态证据；未安装、运行目标 APK。当前动态 App 网关与运行配置一致。官方落地页 HTTP 错误如实保留，不使用第三方下载站替代官方来源。
- 同一业务域名、同一会话的受控直连探针：完整赛程 2,075 场，36 场盘口分三批全部成功。没有再次口令登录、切换代理或投注；该证据反对直接将 0401038 归因为域名失效，但不宣称测出服务端限流阈值。
- 修复整轮请求扇出、单场回退无预算及单批限流丢弃完整赛程的问题：默认并发一、间隔一秒，每轮 96 场盘口预算、滚球/临近/远期轮转；保留有效快照，限流停止后续请求并冷却。WebSocket 已恢复约 2,058 场订阅。
- 已部署批次互斥，随后实测远端 PostgreSQL 往返约 0.58 秒，旧逐条写入仍造成超时及锁等待；正在验证整批赛程一次 upsert、盘口管线写入并保持旧事件拒绝和完整快照关闭语义。尚未把该批量版本标为真实联调通过。
- Docker 冻结回归：首轮 473 项 / 180.801 秒通过；加入上传互斥后 474 项 / 180.849 秒通过，命令均为项目隔离验证脚本，退出码 0。最新批量实现正在进行冻结回归，新增混合新旧赛事和重复输入原子拒绝用例；最终计数待记录。
- 当前分支 fix/TASK-76-leyu-bounded-recovery；下一步为批量写入回归、部署后新鲜度实测、报告整理与 GitHub 主线合入。Telegram 会话重复及正式赛果输入仍为独立未验收项。

### E97 真实恢复与边界补充

- API 批量入库部署后，远端投影达到 2,119 场；公开比赛接口在 12:56:01 UTC 返回 HTTP200、60 场滚球足球、1,059 条摘要盘口、1.633 秒。默认 15 秒交易新鲜度门禁在该时点未通过，不把页面有盘口等同于可下注。
- 12:56:07 UTC 新采集进程窗口：2,032 场 WS 订阅、队列 0、丢弃 0、写入失败 0；源事件到入库 P50/P95 为 3.460/6.586 秒，分别 59 个交付样本。更早窗口曾有超时和丢弃，已保留，不用重启后的累计值掩盖。
- 最终批量 API 加混合新旧、重复原子拒绝场景的冻结回归 477 项 / 185.759 秒通过，退出码 0；后续再将元数据批次从 50 提高为 250，保留 2,000 报价限制，新增 501 场批次边界。定向 46 项 / 0.249 秒通过；最新冻结全套仍在执行。
- Docker 内 8 个改动 Python 文件 AST 语法检查、Compose 契约、Git diff 检查均退出码 0。报告离线双端及 file 直接打开通过，先前手机长哈希溢出和过强滚动断言的失败已保留并修复。

### E97 提交前验证冻结

- 最终部署实现（包含 250 场元数据批次）的 Docker 冻结回归：sh infra/test/run.sh，477 项 / 182.094 秒，零失败、错误、跳过，退出码 0；证据目录 .runtime/test-evidence/2146095/，完整冻结源哈希保留。
- sh infra/check-runtime.sh、Docker 内 8 文件 AST 语法检查、python tasks/check_planning.py 和 git diff --check 全部退出码 0。规划结构共 46 个工作包、332 动作、206 个有证据勾选项，不声称未勾选节点已经完成。
- Docker 浏览器离线报告检查桌面 1440、手机 390 和 file 直接打开三场景通过，退出码 0；无外部网络请求、图片解码正常、正文无横向溢出。下一动作：创建 PR，运行远端干净检出 CI，全绿后合入并校验 main。

### E97 GitHub 主线与真实页面验收

- PR #76 的运行 40508233916：backend、browser、android 三项 success，检查命令 gh pr checks 76 退出码 0。13:05:17 UTC 合入 main，合并提交 1fd96ce85b0fa419e5158c8c78dd56ba5f4852f6；git pull --ff-only 与 git ls-remote 核对本地/远端一致，退出码 0。
- 远端干净检出回归原始证据已下载项目内，摘要与范围入报告。当前 API 入库模块及 Collector 两模块 SHA-256 与提交工作树一致；不以镜像标签相同代替源码核对。
- 真实公开只读页面 1440/390 双端加载 50+ 场、无 JS 异常、无整页横向溢出，命令 Docker 内 live-smoke 退出码 0。实际手机截图发现盘口与长队名局部重叠，另列后续布局验收，不能把烟测冒称视觉全通过。
- 13:02:27 UTC 公开接口返回 79 场滚球足球，6 场带符合默认 15 秒门禁的报价；之后实时状态继续 partial，不降低门禁。Issue #72 已发布本轮恢复、验证及剩余依赖，保留 TG、官方赛果和正式签名等未完成项。

### E98 — 用户指定采集 CF 中转（2026-10-27 UTC，进行中）

- 用户要求比赛与盘口 HTTP 请求通过指定 HTTPS 中转。当前直连状态为 partial、无访问拒绝或限流，约 2,061 场赛程和 2,039 场 WS 订阅；没有证据可直接认定服务器 IP 被封。较长运行窗口出现队列丢弃 1,988、当前队列 0、写入失败 0，该积压问题单独保留。
- 不携带凭据的连通探针：中转根路径 HTTP404，域名路径及完整 HTTPS 路径两种格式均返回 HTTP200 / 0401013（未提交会话），说明可达上游授权校验，尚未证明带授权采集成功。
- 设计：独立可热修改的采集中转来源，保留原业务域名，通过 HTTPS中转来源/原域名/原接口路径路由；仅用于完整赛程、批量盘口及单场盘口。App 登录、登录代理、WS、余额及订单保持既有链路；保留预算、限流冷却与禁止重定向策略，不在拒绝后自动换出口重试。
- 当前分支 fix/TASK-77-collection-relay。下一步实现请求路由与设置，补正常及异常场景，在 Docker 验证后进行有界真实探针并部署。中转服务可见业务会话，App 登录口令不会发送给它。

### E98 已部署与隔离验证

- 13:37:00 UTC 中转探针：2,069 场赛程、12/12 完整盘口、2.497 秒，无失败或限流。由既有管理员鉴权 API 保存，配置版本 6 到 7，真实采集回执 7、状态 applied；业务来源和登录代理未修改。
- 13:46:08 UTC 中转采集：2,091 场赛程、96/96 完整盘口、零失败批次或最新错误。WS 直连订阅 2,089 场，当前队列/丢弃为零；启动窗口累计 8 次投递重试失败后恢复，如实保留。公开 API 为 122 场滚球足球、2,594 条摘要盘口、HTTP200、2.028 秒，源时间门禁保留。
- Docker 定向 65 项 / 1.344 秒通过；冻结全量 sh infra/test/run.sh：483 项 / 183.501 秒（外围 185.380 秒），零失败/错误/跳过，证据 .runtime/test-evidence/2235563。浏览器 sh tests/browser/check.sh 26 场景、sh infra/check-runtime.sh、22 模块 AST、node --check 及 git diff --check 均退出码 0。
- API、Collector、Web 已重建替换。下一步提交 PR、远端干净检出 CI、合入 main 并核对引用。脱敏快照与边界说明入乐鱼协议验收文档，未将独立的旧报价、Telegram 或官方赛果验收标为健康。

### E98 主线合入与连续采集验收

- PR #77 在 13:53:10 UTC 合入 main，合并提交 5fc8cd3a9ac32e708b45476c3be5814a222d3aa8；远端运行 40515361238 的 backend、browser、android 均 success，gh pr checks 77 退出码 0。git pull --ff-only 与 git ls-remote 核对同一主线提交，退出码 0。
- 远端干净检出后端回归 483 项 / 108.015 秒，零失败、错误、跳过，committed_source_verified=true；原始证据下载到项目运行目录，脱敏 CI 摘要见 evidence/collection-relay/github.json。
- 13:52:06 UTC 连续运行仍为 https_relay、配置版本 7：2,086 场赛程，96/96 完整盘口，失败批次 0、未限流、最新错误为空；WS 直连 2,068 场，队列和丢弃为 0，启动时的 8 次投递重试失败仍保留。数据源状态 partial，既有报价新鲜度及官方赛果缺口未伪装成通过。
- 部署后的真实网页只读烟测：桌面 1440、手机 390 均加载 50+ 场，无 JavaScript 异常或整页横向溢出，命令 Docker 内 live-smoke 退出码 0。此结果不代替全部局部视觉验收。网页仍监听 0.0.0.0:3003；API、Web 健康。
- 辅助读证据时宿主机 jq 不存在，初次非 root Docker 读取受保护运行目录被拒绝；改用只读挂载的 Docker 标准库后成功。失败仅属取证工具调用，不改写为已通过，也未改目录权限。

### E99 — Hermes 旧模型配置复用与 Telegram 多群自动同步（进行中）

- 用户授权继续使用旧项目 LLM 配置，确认模型具备图片能力；要求同时监控多个群组、新来源自动同步管理员，并同步已信任专家的最新 Telegram 昵称。稳定身份编号及历史收益不能随昵称变更。
- 当前分支 fix/TASK-78-hermes-telegram-sync，基线 8a13716。先核对现有配置映射、Hermes 专家技能加载及视觉工具回路；再实现远端持久的自动管理员目录与可信昵称同步，群组失败隔离、保留手动信任选择，不擅自将所有管理员开放为可信投注来源。
- 已发现 Hermes 日志存在 expert-review 技能未找到，容器默认工作目录 exec 失败；后续使用显式工作目录排查，不将配置声明 supports_vision 当成真实视觉调用证据。现有 Telegram 采集已有每群锁及三路并发，仍需验证自动目录与昵称刷新。
- 验收计划：Docker 正常/失败/分页/多群隔离测试；真实已授权 TG 只读目录和昵称同步；真实 Hermes 图像及 MCP 工具调用；冻结全套回归、部署、PR 与主线核对。全程不执行真实投注。

### E99 功能扩展及首轮验证

- 用户追加普通成员搜索：独立成员目录接口与“所有成员 / 仅管理员”选择已实现，原管理员角色筛选逻辑不变。按当前群的稳定账号编号搜索，普通成员选中后使用原信任作者映射接入监控；用户名精确查询先验证该群成员身份。
- 管理员目录在每群采集租约内自动分页同步，新增来源首次执行、既有来源每五分钟刷新；手动可信集合改变可触发刷新。昵称按稳定编号更新、保留别名历史，不自动信任未选择管理员。目录受限/限流保留上一份完整快照。
- 旧环境与 Hermes 当前提供方地址、访问键均匹配；模型保留用户指定 cline-pass/deepseek-v4.1-flash。重建容器恢复了失效的旧工作区挂载，Hermes 已 healthy；实际图像色块请求 HTTP200 / 3.705 秒、识别顺序正确，框架会话轨迹确认 skill_view 被调用，不凭模型自述验收技能。
- 多群并发与普通成员定向 52 项 / 0.963 秒退出码 0。首次全套 493 项有两处数据库写入失败：时间参数类型推导冲突，已加显式类型转换；浏览器首次两个预期不符（完场文案、默认搜索范围）已纠正，28 场景重跑退出码 0。失败日志保留，最终冻结回归待完成。
- 比赛状态切换按完整筛选键清空旧行与游标，同筛选刷新失败才保留旧结果；旧请求和联赛响应不能覆盖新筛选。新增等待、失败和普通成员选用浏览器行为验证。

### E100 — 历史遗留项闭环、完整点击测试及优化（2026-10-27 UTC，进行中）

- 用户追加要求：完成历史遗留问题后执行完整功能测试及真实浏览器点击测试，再进行一轮优化；沿用已授权的并行代理和持续任务记录，不把先前隔离验证冒充真实联调。已按当前职责分配采集与健康、Android 与边缘发布、赛果与统计三条独立工作线，主代理继续处理 Telegram/Hermes、任务证据盘点与浏览器全流程。
- 当前主清单仍有未勾节点，先核对“实现缺口”和“已有证据未登记”两类；外部签名、受控设备、支付主体及官方赛果不足不得以造假勾选。真实投注、生产测试假消息与整库清理不在本轮验证动作内。
- Telegram 新部署已确认四个来源检查点均有有效租约，自动目录远端落库：两个来源 ready（7/11 名管理员），另两个来源 member_enumeration_restricted；已保存四条可信昵称历史。受限目录不会阻塞其他群监控，也不伪装成空成功。普通成员路径已由真实浏览器及协议样本验证；当前这四个真实来源未返回普通成员，不能据此声称已在生产搜到普通成员。
- 下一步：逐项补实质代码缺口，核对历史验收证据；在所有代理结束写入后冻结完整回归，运行真实浏览器点击与故障恢复，记录发现并优化，再进行远端 PR/CI 与主线核对。

### E100 第二轮实质修复与失败留存

- 全套首次冻结525项 / 196.884秒，退出码1：唯一错误为恢复测试清理跨角色连接时使用强制删除；未将该轮记录为通过。存储工作线改为关闭本例连接、核对随机临时库归属并有界普通删除，不提升业务账号权限。
- 真实只读存储检查确认远端PostgreSQL可读、S3桶及限定前缀可读；首轮S3读取超时保留。当前数据库账号仍有superuser/createdb/表所有权，最小权限验收明确失败，未擅自改远端角色。
- 发现执行器非root用户无法读签名挂载：项目内原文件改为root及专用组、0440，采集器和执行器持久配置附加组；两服务可读且无组用户被拒绝，均退出码0。旧容器待统一重建后再次验证。
- 浏览器第二轮29项场景通过，命令sh tests/browser/check.sh，退出码0；包含五个比赛状态真实点击、切换失败不残留旧状态、统计窗口一致及普通成员搜索。先前360宽度专家页筛选溢出已修复并重跑。独立真实API隔离12项与辅助功能检查均退出码0；实际部署页面完整只读点击仍待最终部署。
- 新增官方回执证据链：官方成交本金/赔率/币种/派彩/下单与结算时间贯穿执行器和API，和预留本金/赔率分列；未知时间不再填当前时间，未知费用保留未知。回执去重留存独立证据，不使用真实投注验收。
- 新发现并继续修复：模型及报价秒数保存后运行时不读取、注册缺少邮箱限流桶、管理事务等待锁后会话未复核；专家质量统计重复计算导致10k合法样本约0.6秒P95，已完成同数据物化CTE等价试验，正在实装复验。性能环境256MiB临时PG磁盘满导致MCP第二页500，保留PANIC证据，不能归因为分页接口错误。

### E100 完整回归与二次优化（进行中）

- 冻结全量 `sh infra/test/run.sh`：571项、213.394秒、零失败/错误/跳过，退出码0，证据 `.runtime/test-evidence/2713458/`。此轮在数据库最小权限工具完成之前冻结，新增工具有独立32项回归及真实CLI隔离验证，不混称已纳入571项。
- 模型与报价运行配置、注册双维限流、事务锁后管理身份复核、准确代理来源边界已修复。专用edge网络仅Web/API，Web已脱离业务私网；部署仍为0.0.0.0:3003。
- 官方订单价格、本金、币种、派彩、费用、时间及收益依据贯穿回执与本人列表；无官方字段保留未知。重复任务结果按摘要幂等，冲突回调拒绝；账户重启与冲突专项31项退出码0，无真实下注。
- 统计在相同10k推荐证据上HTTP P95为388.835毫秒；SQL等价对照从499.059降至296.588毫秒，原始行结果完全一致。250候选十页MCP检索通过；测试PG磁盘预算问题单独修复，无截断或放宽匹配门禁。
- 真实浏览器首轮失败包含测试脚本错误：隐式提交按钮被误写为显式submit、手机误点隐藏桌面入口；修正选择器重跑，不当成产品修复。真实手机截图另发现多盘口及“更多”按钮与队名局部重叠，改为独立网格第二行，加入360/390/720长队名四盘口几何断言。首轮密度断言失败后调整常规行78像素并保留自动增高；30场景重跑退出码0。
- 远端迁移016前已保存仅v2表的私有dump、SHA-256及pg_restore目录验证；迁移退出码0，当前44张v2表。最小权限只读plan为ready，开始按独立新运行身份切换；旧迁移身份、对象所有者、旧表及PUBLIC ACL不修改，实际切换证据待补。
- 正式赛果REA审计确认请求契约，但正确请求仅返回成功空数组，日期查询超时；同会话赛程2512场成功。缺非空赛果与修订规则，不生成猜测confirmed/void。Android正式签名和授权设备升级仍无实际证据，开发签名流水线通过不能代替正式发布。

### E100 运行切换及真实点击复验

- 全部源码再冻结回归 `sh infra/test/run.sh`：582项、227.596秒、零失败/错误/跳过，退出码0；证据 `.runtime/test-evidence/2739688/`，已包含运行角色11项新增用例。浏览器行为30场景、实际API临时数据库12场景、辅助功能双端扫描均退出0，独立真实部署点击1440/390共10组退出0，零页面JS错误。覆盖五种赛事状态、日期、真实盘口详情、专家窗口、管理登录及六分区、四个TG目录、普通成员搜索入口、会员和个人中心。没有保存业务设置或实际下注。
- 实际创建独立运行角色并通过schema及摘要检查，API/审核工作器已切换；运行探针superuser/createdb/createrole及持久schema CREATE均false，拥有0表，44张v2表。旧迁移身份保留，新角色只可DML；旧表及PUBLIC ACL未修改。Compose、边缘配置及actionlint退出0。
- 角色应用第一次verify发出过早、切换脚本模块路径及误将016当16份迁移的两次错误均保留；实际定义15份迁移，切换前校验未通过时未修改私有配置。新配置合并后只重建API/审核/Web，其他设置不变。
- 实际S3 HEAD后前缀LIST首轮3秒读取超时；同一只读探针重试成功1207.9毫秒，连同数据库总2394.8毫秒，least_privilege门禁通过。没有提高超时或访问旧前缀，尾延迟证据保留。
- 17:32:53 UTC后台仍有376条排队、2条运行、2条重试，最新Hermes成功结果17:32:49，赛程及盘口分别17:32:50/42更新，四来源租约有效。不能把后台有进展说成无积压；Telegram检查点、共享作者锁和远端入库在切换窗口确有2—5秒超时及500，正在补正确503和锁争用优化。
- 137个Python语法、前端三脚本语法与候选文件秘密/产物扫描退出0；规划46工作包337动作222项有证据勾选。扫描首次非root无权读私有目录、verifier无Git命令已纠正为宿主生成仅路径清单与容器只读解析，不放宽私有目录权限。
- 下一优化明确限定：数据库可恢复超时响应、Android权限精确白名单与设备包身份验证、受保护正式发布计划校验；各自独立测试后再冻结回归和远端PR，不将缺设备或正式赛果的节点冒充完成。

### E100 Git阶段提交与锁争用修复

- 第一阶段提交4a847a6已推送任务分支，`git ls-remote`与本地完整SHA一致，退出0；Issue #72已更新真实验证、失败与剩余项。未直推业务代码到main，后续优化单独提交审阅。
- 真实PG复现锁链：专家行被占用→另一来源同专家无条件昵称UPDATE等待→该事务已持来源检查点锁→续租UPSERT两秒超时。移除同事务重复昵称UPDATE且同值不写，不拆事务、不放松租约。8项新PG/HTTP测试与2项原脱敏测试均退出0：同昵称遇NO KEY UPDATE仍可入库，真正改名冲突完整回滚，恢复只生成一条消息及审核任务。
- 明确可恢复数据库异常统一503、Retry-After=2、no-store和请求号；写入结果标unconfirmed且automatic_retry=false，普通非DB超时、约束及既有HTTP码不变。没有增加超时或自动重放未知事务。API及审核工作器已重建健康，实际后续检查点与队列观测进行中。
- 正式赛果追加静态日期契约及三次只读：两次日期查询超时，全体育空日期返回一条排球记录；异常未来时间和全零比分不作为足球确认或作废。正确契约证据已扩展，足球及修订缺口仍保留。

### E100 第三阶段优化与真实积压根因

- PR #78已建草稿，业务修复84d92d8、Android发布契约0c3dfd6分别提交并推送。Android定向26项、四CLI、工作流检查均退出0；开发包与签名候选共用两网络权限精确白名单，设备验收使用实际包身份/哈希/版本链，正式发布plan/validate只读校验六镜像及八域证据。未冒充真实安装升级或正式签名。
- 17:40:40 UTC真实只读审计发现367条queued中366条发布时间早于24小时、最旧约3.78年，所有记录符合原调度条件；240个框架结果、734次搜索中489次零候选，5次提交全部因过期或变化报价拒绝，当前信号0。该证据否定“模型有调用即有有效推荐”的说法，已安排持久实时/历史分预算调度，保留历史数据且禁止历史消息绑定当前报价。
- Telegram初次历史补拉每群持锁最多1000条且同步媒体，实时回调共享同锁；改为2秒/25条软片预算，持久推进后释放，后续仍从高水位继续，不截断总历史。新增跨片无遗漏、实时事件插队但不推进历史游标、时间片断点测试；30项采集定向退出0（首次5秒/250条，现常量收紧后随全套再验）。单条网络截止仍需独立核验，软片预算不能声称硬延迟上限。
- 真实检查点随后已有三来源推进，另无专家消息来源保持旧时间，不把旧checked_at一概判成死锁。最新盘口源时间在15秒门禁内为0、938条market_reads读入时已过期，正独立审计源时间与补采预算；不扩大TTL、不用received_at替代未知源时间。

### E100 第四阶段：历史分流、媒体公平与报价修复

- 最终上一冻结全套604项、226.088秒、退出0，证据`.runtime/test-evidence/2777355/`；这是017及本段新修复之前的基线，不能作为本段最终验收。PR #78上0c3dfd6的backend/browser/android/contracts四门禁均通过；后续补丁待新CI。
- 审核按原始发布时间分实时与历史回放，默认24小时窗口、8次实时配1次历史、历史全局并发1；持久预算跨重启，保留来源作者公平、租约与原文。新增11项加26项既有Docker测试退出0。MCP录入同时校验模式及原始发布时间，未来消息和历史消息不能生成当前推荐；Hermes独立中文约束及技能同步说明。
- Telegram历史每片2秒/25条，1005条持续续扫不截断；实时、补拉、旧复核先持久pending，媒体传输锁外进行。单条20秒、批次30秒、提交5秒截止，每轮一次重试；上传前登记确定性对象，提交前复核租约与版本，超时或旧版不ready。43项采集专项退出0；API保留并回传候选对象，过时failed候选仍可被原孤儿清理索引。
- 确认ctsp为变价源时间，未发现秒毫秒解析错误；确定旧完整HTTP绕过单调合并会回退新WS并污染下一条缺状态推送，已统一同键真实源时间比较，保护请求进行期间新增WS的删盘竞争。73项专项退出0。另健康统计原硬编码60秒与实际15秒不一致，已使用后台当前阈值热更新，读取失败unknown，30项专项退出0。
- 精确刷新只接受当前任务唯一绑定的赛事，事务外调用已有采集控制端，不占数据库连接；刷新后重新验证任务、比赛、报价及原有门禁。历史模式不触发刷新，上游成功但ctsp仍旧不得变fresh。首次MCP新测试暴露持久列名误写source_match_id，已更正为source_id别名，保留失败日志；随后MCP19项及TG API19项退出0，新增租约及刷新成功路径随最终全套再验。采集端共享冷却/节流及单场请求仍在定向验证。
- 017前v2备份10,626,286字节、SHA256=a9a831d72a09f1d23bec2acea73d3c96a0acff6bdd67973962dda157859026f7，pg_restore目录验证退出0；暂停审核后017 applied_count=1退出0，运行身份refresh/verify均退出0，当前45张v2表/44业务表，旧迁移身份及旧数据不变。新API/审核已启动，collector最后补丁待统一重建。
- 18:05:35 UTC探针仍有538queued、313unresolved、3running（含旧尝试租约），4来源中3有效租约，最近盘口18:05:30；新调度不删除旧积压。采集仍partial、TG有HTTPStatusError，不能据启动健康宣布外部采集全部恢复；部署新媒体补丁后继续观测并真实点击。

### E100 部署后验证与完整回归夹具修复

- 新API、审核和采集均已部署；18:07:34 collector早于18:07:56 API启动，导致内部ConnectError，旧owner租约交接还产生检查点409。只读复核确认18:08:59后409消失、四来源租约有效；随后赛事81次、检查点79次、消息73次HTTP200，API无500/503、DB无锁等待；未因部署暂态重登或清租约。
- 新部署真实浏览器1440/390两宽共10组点击通过、退出0：五赛事状态、日期/详情、专家筛选原文入口、管理登录六区、四TG来源管理员、普通成员搜索、会员/本人及溢出检查，零JS错误。隔离30场景、真实API临时库12场景、辅助功能双端检查均退出0。生产读取不保存设置或下注。
- 当前精确刷新collector新增29项、关联共159项离线退出0。真实仅一次授权当前比赛探针HTTP200返回refresh_busy，未发重复请求或登录；该结果只证明控制端联通及并发保护，不能冒充报价真实刷新成功。
- 本轮冻结全套首次退出137，无成功报告；日志精确247个测试点后为旧限流恢复测试。新增共享冷却遇旧模拟wait立即返回但不推进monotonic，导致测试空转并由AsyncMock积累调用占用内存。根因已定位为不真实时间夹具，不扩大测试内存、不关闭生产冷却；修正假时钟并重跑全套，失败日志保留。
- 当前150个Python语法、408候选文件秘密/产物扫描无命中退出0；Compose假配置边界和特殊字符容器验证退出0。生成截图与结果未混入源码提交，保留真实点击的私有证据副本。

- 随后冻结完整回归687项/248.509秒退出1，错误2项，均为恢复目标空库检查：017新增审核预算种子行被当成已有业务数据。未修改生产数据库或跳过恢复测试；专门修复初装种子识别，并保留任意业务数据拒绝覆盖及原预算恢复验证。
- 新任务台账人工证据复核完成：46包337动作中231有证据勾选，不按历史要求机械补足数量或全勾；正式签名/设备、官方足球赛果、语义金标、真实支付及历史清理仍保留未验收。

- 精确刷新真实两次refresh_busy后继续改进：旧状态字段只描述上轮完成而非当前锁状态，且“见锁即忙”没有排队资格。改为同一asyncio锁FIFO等待，原请求总8秒截止，等待到期不留后台任务、不取消已有轮询、不额外发请求；新当前轮询开始/完成/进行中字段用于可信诊断。90项相关离线测试退出0，当前统一重建验证中。

### E100 第四阶段真实刷新成功与恢复保护

- 恢复机制只豁免迁移017从未使用的预算种子：singleton为真、计数零、更新时间精确等于迁移应用时间。已使用计数、清零但更新时间变化及任意其他业务数据仍拒绝覆盖；源预算6及原时间完整恢复。Docker定向14项/22.772秒、退出0；原两项失败已原样重跑通过，未修改已应用迁移或生产数据。
- 最终FIFO镜像已先API健康再采集/审核启动。18:24:53 UTC单次授权当前赛事精准刷新HTTP200，accepted=true、written=true、written_fixtures=1；同场248条报价中11条的真实source_updated_at在15秒窗口内。源最新18:24:46.780、接收18:24:51.949556，首次验证真实源请求→远端入库→重新读取新鲜报价链。没有登录、代理切换、扩大TTL或投注；精确样本成功不等于全站覆盖或长期可用。
- 18:24:52 UTC四来源租约均有效，TG最近成功18:23:38；赛事2552、接受2543、正常轮询5次成功/0次失败。审核仍571 queued、318 unresolved、2 running、3 retry；旧积压未丢弃。WS已连接、队列0、丢弃0，但累计投递失败3；TG最近source_unavailable，采集partial，不将容器存活写成上游全健康。
- 第三次最终冻结全量已启动，保留前次退出137与687项两错误日志。真实部署双端最终点击并行执行，后续用实际结果补证。

- 第三次完整冻结回归 sh infra/test/run.sh：693项、257.085秒、零失败/错误/跳过，退出0，证据 .runtime/test-evidence/2873554/；完整恢复前后45表媒体、密钥及资金指标difference为空。最终部署双端真实点击10组全部通过、零页面JS错误、退出0。语法150个Python、408候选文件敏感/产物扫描无命中，规划46包337动作231完成、退出0。
- 验收后的稳定观察又发现实际阻断，不能停止在“测试绿”：同场盘口超过500触发fixture_quote_budget_exceeded，WS投递失败和队列丢弃增长；TG受内部请求超时影响。新任务优先修完整有界快照协议，不截断比赛/盘口。另审计发现缺盘口线被默认零的契约缺口及故障恢复矩阵缺失，已分配独立实现，前次693只作其之前基线，修复后重新冻结。

### E100 稳定窗口发现后的第二轮优化

- 单场超500盘口不是上游拒绝：采集发送前主动抛fixture_quote_budget_exceeded。新增有界完整快照合同：每片不超过500、同场/批总量不超过2000、UTF8请求不超过4MiB；所有片同一HTTP同一DB事务，禁止截断或多请求末片误关旧盘。超硬预算只隔离该场，保留其他比赛投递。collector 98项关联专项退出0，API原子入库专项进行中。
- 新增管理员逐项待办，将会话、图片、未解决审核、过期报价、未知订单和迁移状态关联实际分区；失败迁移历史没有证据时固定未核验。API7项+转换3项退出0，浏览器30场景退出0，新增按钮不会污染主导航选择器。
- 真实临时PG短断注入暴露transaction退出InterfaceError掩盖连接丢失而返回500；仅沿有界防环的typed cause/context识别明确连接异常后503，其余编程错误仍500。首次故障矩阵失败留存，修复后实际断连503/回滚/重放唯一已通过，完整矩阵待盘口契约冻结复验。
- 支付状态与人工补偿设计已独立补齐，不以缺商户阻止可完成的设计；在线支付仍未配置且不伪造成功。当前约束与稳定记忆同步后续目录授权、管理员MFA按用户关闭及PR78状态，去掉与已授权动作冲突的过期记忆。

- 018前只读聚合：177256条当前报价中独赢非零、非法数值、空线及语义唯一键冲突均0；推荐及证据目前均0。私有备份11750076字节，SHA256=38e4ace31204c6f035231be39bbd83cbfe02c0ae98ced036ef8b2b943812ba68，pg_restore目录验证退出0；尚未执行018真实迁移。
- 新故障矩阵及关联17项退出0：实际临时PG断连、S3迟到恢复、TG限流边界、模型429新尝试、MCP已提交丢回包唯一。该次复制冻结快照不含随后编辑的018，完整最终回归仍需重跑。恢复顺序、端口冲突和归属文档已补；远端全部服务归属与无标签遗留资源仍不冒充已验。

- API空线与原子快照包179项/104.417秒退出0：2000报价在原5秒语句预算内、缺片/重复/超限拒绝、最后片故障整事务回滚、独赢null经MCP幂等录入/证据/订单可用/结算链通过。018旧行预检后建立完整已验证约束，未采用NOT VALID掩盖异常；SHA31a3f4f551d4d2d2f01360146802f67e5ebc7913ecc870f6c0858cb035c1be24。
- 再发现WS合批同盘口多事件重复append会造成快照超500及新API重复键拒绝，已按同语义键只保留最后接受且源时单调的完整报价，不截断不同盘口；仅明确独赢旧0与null同义，数字玩法缺失仍不等0。85项专项退出0，包含501同键和501异键，最后collector镜像重新构建。
- 受控暂停api/collector/review-worker/executor写入后018 applied_count=1退出0；运行角色refresh/verify均ready、verified、无旧表权及持久DDL。API先healthy后其他项目服务启动，网页仍0.0.0.0:3003。当前候选镜像已构建并运行，待新窗口确认，不以迁移成功冒充稳定。

### E100 完整回归前新增缺口闭环

- 订单许可复用原事务追加preflight_validated与submission_authorized两份白名单不可变证据；相同存储摘要幂等、冲突409，HTTP许可不重放；第二阶段失败完整回滚，未知发送保留敞口。新增8项与关联68项53.822秒退出0；全部原生请求为隔离替身，无真实下注。
- 真正多进程启动补验：独立迁移后2个uvicorn和1个审核工作器同时启动，两个readyz200、工作器SQL轮询推进，低权角色无DDL、前后结构与版本一致；未迁移库3者拒绝且不建表。2项19.630秒退出0，PG与验证器总512MiB预算，结束无残留连接。
- 真实三窗口18:53:09/29/49 UTC：滚球15秒新鲜报价0→16→0；盘口源至接收存在1.073秒合法样本，WS零失败/零丢弃。多数盘口源时间确实陈旧；外层事件延迟不能代表盘口延迟。继续修复旧父比赛时间吞独立新盘口、补盘口级滚动指标，禁止用接收时间刷新ctsp。
- 18:53第二个旧汇总探针因采集控制端4秒ReadTimeout退出1，保留空输出和错误，未冒充零错误；独立只读三窗口仍有采集及检查点数据。最终冻结需包含本段所有新业务源码及完整浏览器回归，693项旧快照仅基线。

- 最新全套771项309.882秒退出1、仅1条断言失败：原017升级用例硬编码只应应用017，但当前016旧库正确连续应用017及018。修正为逐一核对所有大于016的真实迁移列表，仍检查017先应用、原任务尝试/租约/历史原文逐字段不变及重跑零迁移；不改生产迁移、不跳用例。随后重新冻结完整套件。
- 旧父比赛版本吞新盘口已修复并经77项46.274秒退出0；既有finished/cancelled不重开，旧完整快照不能关缺项，真实比分锁竞争保护。新增盘口级时延9项与既有共41项退出0，独立有界窗口区分unknown/invalid/future/too_old。API、collector、审核与网页已按先API健康后其他服务更新，等待最终真实窗口。

### E100 最新完整冻结与浏览器验收

- sh infra/test/run.sh第二轮完整771项、unittest303.372秒（机器报告含加载305.461秒），零失败/错误/跳过，退出0；证据.runtime/test-evidence/2977088。首次771项309.882秒的一条迁移列表夹具断言失败保留，已原场景重跑；逐文件SHA核对被测后端源码无漂移，退出0。
- sh tests/browser/check.sh30场景退出0；sh tests/browser/integration-check.sh17场景及82次真实HTTP断言退出0；sh tests/browser/accessibility-check.sh双端固定页面零已确认规则违规退出0。生产网页1440/390共10组真实点击退出0，零JS错误，覆盖全部阶段、日期详情、专家、管理六分区与四TG目录/普通成员搜索、会员及本人页；没有保存业务设置或下注。截图和结果保存在私有证据目录，不混入业务提交。
- sh infra/check-runtime.sh、actionlint、git diff --check及164个Python语法/439候选文件秘密和产物扫描均退出0，无敏感/产物命中。新盘口指标已接管理白名单和页面，明确成功投递不等于每条盘口被数据库接受。
- 19:10:05 UTC四来源租约均有效，最新检查点19:09:50，TG最近成功19:09:25且无当前错误；正常轮询7成功/0失败，WS已连接、队列0、丢弃0、投递失败0。队列仍589 queued、333 unresolved、2 running、4 retry；最近框架结果19:07:35。不把连接恢复与旧积压正在消化解释为模型语义或盈利通过。
- 只读存储规模报告已入库：数据库173.358GiB，旧模型两表95.234%，当前白名单90.344MiB；184条关闭报价仍有审核引用。旧18表无读取权限，软引用未知，所有历史数据保留。分区/批写/缓存及保留假设是设计，未凭累计WAL归因当前IO。
- 任务证据对照补齐原有已验收的领域契约、索引/超时、真正多进程启动、订单两阶段证据及管理员待办；只勾有范围与证据的节点。正在推送PR78新一批，随后以干净检出CI与主线摘要复核；Android扫码和先关闭详情的返回行为另包实施，正式签名/设备与官方非空足球赛果不冒充完成。


### E101 复核后继续补齐而非结束在绿灯

- e29e1ec已推送任务分支，远端Android、backend、browser、contracts四项均退出0；backend5分38秒。PR78草稿暂保留，新独立功能包验完后统一合并并核对main。首次gh pr edit受旧GitHub Projects GraphQL接口拒绝，改用受支持REST更新，失败不当发布成功。
- 核对运行源码发现executor携带旧采集模块副本；已按本项目构建并重启executor，退出0；其余API/审核/采集源码摘要已匹配。此操作未改其他服务或资金开关。
- 后续只读三窗口19:18:58、19:19:18、19:19:38 UTC均控制端200，WS持续连接、零丢弃/投递失败，256样本quote源至接收P95=6105.4ms、成功投递年龄P95=10949.7ms；但三次活跃报价15秒内覆盖均0，不能称持续新鲜或盈利恢复。TG四来源，最后窗口三有效租约，持续推进另测。较重聚合此前5秒语句超时已保留，改为小查询而不扩大生产超时。
- Android扫码与优先关闭HTML详情的新包23JVM/TLS、6真实Chromium及26发布契约退出0；无受控设备，真实扫码/WebView手势及正式签名未验。保留演练35项81.223秒退出0，仅临时恢复库；旧migration身份只读能查旧表，运行身份无权不等于用户无授权，软引用未知仍保留生产历史。
- 下一实施包：TG首次接管稳定身份核验；赛事项目入检索与游标；不可变工具白名单返回快照及失败计时；确定性简体中文系统回执与保留模型原文；不可覆盖的新开发候选发布。各包独立专项后再次冻结全套和浏览器，未执行真实投注。

- E101独立包已冻结：TG稳定身份接管104项1.467秒、Android23JVM+6浏览器+26契约、隔离保留35项81.223秒、不可变候选发行25项及actionlint全部退出0。优先单独提交这些已验包，后续审核工具/中文回执/原文轨迹分页及配置审批仍在独立开发，不把正在修改的源码作为本次冻结证据。
- 中文回执首轮45项出现2项测试夹具错误：claim后仍用旧attempt，被真实409门禁拒绝；修正为本次attempt后仅剩价格小数位字符串差异，改为Decimal精确数值断言后45项39.862秒退出0。换行展示随后收紧，仍需新冻结原场景重验。精确刷新accepted=0明确为父比赛未变、应独立重读盘口，不再误报非法回执；无报价写入计数证据则不宣称已写新价。73项2.409秒零跳过专项退出0；两次测试入口配置错误（不存在模块/缺DB导致跳过）均保留失败，未作成功计数。

- 89a9a69 远端干净检出四门禁全部通过：contracts17秒、Android3分45秒、browser2分、backend5分51秒；本地 ls-remote 已核对。后续在途源码尚未纳入此提交，不能用该 CI 代替最后冻结。中文回执换行收紧重跑45项40.207秒零失败/跳过退出0。
- 配置台账增加同一证据轮次的最新状态，明确旧 AUTH_KEY_DUPLICATED 记录不代表当前四来源未配置，运行低权身份不代表旧数据无授权；P02-10、P08-05、P14-06、P44-02按独立事实补勾，P14-02纠正与用户关闭MFA冲突的旧要求。任务数量不填充，其他真实缺口仍保留。
- 19:50:12/32/52 UTC小查询三窗口均退出0：滚球15秒内报价0/0/0，赛前22/0/0；WS投递4076→4091→4107、丢弃0、累计投递失败1、队列0/1/0，盘口源至接收P95=1891.5毫秒，至成功投递年龄P95=7068.5毫秒（256条滚动样本）。四来源租约4/4/2，TG connected但最后成功19:48:42。不能把增量持续到达说成所有报价新鲜或四群永不中断。
- 原文专项发现主中间件覆盖private响应头，已最小修正所有API成功及错误private,no-store、内部保持no-store；新增四项缓存行为与三项已有数据库HTTP故障回归共7项0.194秒退出0。原文路径与模型治理仍由各自独立包收尾，统一冻结后继续全套、部署和远端审阅。

- Hermes 固定真实框架隔离最终验收 `bash tests/integration/run_hermes_acceptance.sh` 退出0：九项协议单元；正常19请求、恶意25请求各完成260候选十三页及末场次日赛前；图片原生输入、技能、六工具白名单、完整快照重放、伪造比赛409、三层错误鉴权401且provider增量0均通过。length四请求与预算七请求均零推荐，实际技能回合四次；框架两次收尾请求单独计数，未放宽业务预算。重启前后配置/上下文/技能哈希一致，技能写入拒绝。日志SHA256=20767d92c6d1f6f9572d89fcb0f3e17cc0547076ff03b97773cafa3536b78407；只用替身提供方，不冒充真实专家语义金标。

- 原文读取最终冻结：新增七项14.462秒及台账十八项32.951秒组合退出0，管理十七项33.359秒与TG十九项37.836秒同源码各自完整通过；一次四组传输143中断保留，未冒充整组通过。最终真实Chromium31/31退出0，含手机续页、完整返回无预取、独立最新中文回执和管理双Cookie原图。当前源码180个Python/474候选文件扫描无敏感或产物命中，退出0。
- 远端只读资源采样19:57:45 UTC共22.048秒：两核CPU忙碌99.160%、iowait0.568%，可用内存约3.05GiB；整盘读3.3076MiB/s、写9.6761MiB/s、366.881 IOPS、io_time19.884%。分区与整盘不能相加；ps累计均值有postgres/Python占用，尚未逐容器归属。首个十秒SSH探针超时及一处脚本缩进失败保留；增加只读探针截止重跑退出0，不改变生产服务超时或资源设置。不能从此短窗断言历史I/O瓶颈已解决，也不能把上游旧ctsp全归因CPU。

- 新审核阅读/工具证据包已提交并推送 e0b54c19f26912d2664bef87af2d26e7eca3b69e；远端任务分支完整SHA一致、退出0。模型治理冻结专项64项36.418秒零跳过退出0，双端Chromium候选/报告/逐差异/取消及单次激活退出0，并修复已移除作者仍列作当前信任的问题；未激活任何生产新模型。
- 20:04:18 UTC远端17.3秒cgroup增量定位：旧storage-api47.664%单核、旧model-worker22.117%、旧archive20.516%、PostgreSQL90.502%、RustFS4.614%。旧三个业务服务仍在运行，v2源码/编排已不依赖其接口；正在核对真实依赖与写入职责，再按用户废弃旧决策/训练和恢复业务的授权停用旧业务容器，持久库、S3及历史数据保持。Docker stats首轮超时保留，改读具名容器cgroup计数而不以ps累计均值冒充瞬时占用。

- 模型治理包 bd9ba54a8d86159fccebcc693f124af4cf994e83 已推送并核对远端完整SHA，退出0。配置保存区分待审候选、已激活配置与工作器实际消费版本；不以ACK或合成评估冒充真实语义验收。
- Telegram来源限时与旧消息复核包冻结：每来源历史25秒、复核10秒、RPC3秒、10事件完整去重批查，慢来源不阻塞其他来源，超时不生成删除事实；新增10项加原采集/媒体/身份共70项1.638秒、零跳过、退出0。进程内保留待复核片，后台尚无持久领取确认，重启后原5分钟/6小时调度及积压等待仍明确保留，详见来源调度验收文档。
- 账户浏览器最终冻结命令 `sh tests/browser/integration-check.sh > .runtime/browser-account-acceptance-20261027/final.log 2>&1` 退出0：17原场景、5身份82断言与8新增成功流程全绿；新增126次HTTP观察、浏览器errors为空。替身核验1、刷新1、回执1、交易0、失败0；无真实上游或生产写入。前三轮失败保留，最终日志SHA256=083f48245dbe43fc2437848afc48e4e977fe3ea73612dfd7caecc1b4eaa30f9b，冻结副本.runtime/web-integration-3137995；持续集成新增上传本轮账户证据。
- 按用户弃用旧决策/训练及恢复业务授权，核对远端容器ID、命令与v2依赖后，仅停止旧ai_football_storage_model、ai_football_storage_archive、ai_football_storage_api三个业务容器；停止脚本退出0，三者退出137，数据库、RustFS和Redis仍运行。未删容器、卷或任何历史数据。20:06:58 UTC短窗CPU忙碌64.379%、iowait32.908%，读1.517MiB/s、写3.4796MiB/s，不能说IO已解决。20:07:29 v2检查点及入库仍推进、四来源有效、TG连接；乐鱼一次HTTPStatusError保持降级，继续部署后复查，不能以容器健康冒充上游可用。

- 4ec64643cea1e1376fe969e1be38da2e43daaaad已推送、远端SHA一致；干净源码完整 `sh infra/test/run.sh` 872项355.984秒、零失败/错误/跳过、退出0，机器报告含加载358.181秒，证据.runtime/test-evidence/3152593。远端四项CI全部通过：contracts17秒、Android2分49秒、browser3分21秒、backend6分28秒。该快照之后第二轮优化另验，不将本轮基线冒充之后代码全绿。
- 同提交API/collector/executor/MCP/Web镜像已重建、API先healthy再更新其余服务，构建与部署退出0。真实部署Chromium1440/390十组点击全过、页面JS错误零、退出0；首个状态探针5秒超时保留，第二次20:17:35退出0，轮询2成功0失败、WS零丢弃/投递失败、四来源租约有效；TG仍有source_unavailable保持降级。
- 第二轮只读复核发现并继续修复：模型审批专项未纳入CI；旧消息observed先推迟复核但无持久领取确认；媒体仅逐次会话授权而无短期版本票据；历史混合大小写别名搜索和唯一解析不一致；采集/续期仍运行本机未完成远端分工。新增R12逐项跟踪，不将需要真实设备、官方赛果或人工金标的条件伪造为已验。

### E102 全量回归后的第二轮优化

- 精确发行源码26b6ef999ccc946e0c534dbd910d526a4052804d的发行流水线40583108949已全部success：冻结Docker后端949项、零失败/错误/跳过且clean=true，Android、双端固定样本、模型配置、视觉、完整盘口分页及真实API临时库浏览器均通过。PR流水线40583024454验证的是合并检出，不误写成精确发行源。不可变开发候选v2.0.0-dev.20261027.1已实际发布，含六个GHCR摘要与APK资产；仍明确production_ready=false。019前停工作器一致备份14638743字节、0600，SHA256=dc2e7c134070b2255f4688ea07ce151bdbe8c3c542d73d372aa5b565779c55c0；生产019迁移applied_count=1后，同源服务重启完成。
- 新复核发现浏览器脚本每次覆盖已入库截图和结果，导致之后冻结源码clean=false；正在把本轮输出迁入任务独立忽略目录，源目录只读挂载，集成检查取消回填，持续集成改从准确任务目录上传。保留原历史报告材料和本轮失败/成功日志；这项证据隔离变更将单独回归，不能借用此前949项声称新增脚本已验。

- 最终真实API浏览器发现快速点击联赛目录的实际竞态：赛事先返回，联赛200迟到时弹窗永久显示空且后续点击被遮挡。保留首次失败及真实回包延迟复现后，页面改为等待当前目录Promise、明确加载/失败重试，关闭或换场时拒绝迟到渲染。原17场景内新增延迟200、关闭后回包及断网重试反例，不删原断言；`sh tests/browser/integration-check.sh` 新冻结.runtime/web-integration-3253514完整17+8流程退出0，替身核验/刷新/回执各1、真实交易0。全量Python仍运行，测试快照冻结后网页修复不影响后端源码；最终统一源摘要和网页专项仍须补验。

- 旧专家台账隔离迁移演练补原作者/昵称/消息修订与完整原JSON及逐条摘要；有序修订仅最新版本参与统计，作者冲突/原文冲突及旧模型记录严格隔离。临时PG核对分类计数、报告与可核验一单位收益、按币种订单金额、余额及全部v2行哈希，篡改原文/收益整事务回滚。显式旧Fernet到现行AESGCM重包先验证旧key解密及StringSession规范，独立版本与key引用，拒绝同key、错误key及协议；不生成密钥、不读配置、不联网，不恢复已废弃授权。`git diff --check && sh .runtime/legacy-migration-20261027/run.sh` 38项78.220秒零跳过退出0，日志SHA256=b533cb12401412e5a2db188fced4e5b3dafdb4b47501a1275c3f853b1de2ded8；真实历史业务导入仍未执行、import_performed=false，P42生产完整性继续待验。

- 远端采集部署包完成同源摘要、双向SSH私网桥、角色路径白名单、配置版本诊断、严格单工作器切换和反向回滚清单；19项6.013秒Docker专项、真实Compose假配置渲染及实际UID/Unix套接字断连重绑合同均退出0。该包尚未在远端启动，真实SSH重连与源数据推进必须部署后另验，不将partial/stale映射成业务ready。只读生产查询计划20:48:03 UTC：赛事首屏服务器4.049ms/客户端480.189ms、实时领取候选0.377/465.812ms、消息首屏1.272/473.519ms；暖缓存单次无临时块，不外推分位SLO。初次探针表名误用与备份脚本引号失败保留，修正重跑退出0。019前私有v2备份14336344字节、0600，SHA256=d3545ee25171cc891dfad0f6ec06a14a3650d09b80a99d21c11574adb1c91364，目录验证退出0；尚未迁移。

- 再审发现比赛详情仍静默 LIMIT 500，已补每页最多500的签名UUID键集分页、跨场游标拒绝以及明确继续加载，刷新保留已展开范围与期间/主附线筛选。真实PG `sh .runtime/final-acceptance-20261027/ledger-focused.sh` 20项23.462秒零跳过退出0，遍历1201条并测篡改、跨场、非法页长和完场陈旧门禁；`sh tests/browser/match-markets-check.sh` 双视口真实点击1207条无漏无重，503保留重试、筛选与迟到回包隔离退出0。当前盘口为变化投影，分页不声称冻结全场价格，新增低位UUID在下一刷新出现；推荐仍只用任务内不可变报价证据。公众检索同步历史大小写空白别名。CI补主线触发及该分页专项，后续main提交可产生自身证据运行。

- 模型审批专项已接持续集成，独立冻结双视口Chromium退出0，输出JSON与截图；固定样本视觉门禁覆盖14屏，正常重复零像素差异退出0，副本顶部变红的真实比较进程退出1且14屏拒绝，未修改产品样式或提高阈值。正式发行执行器增加外部签署、受保护主线与八范围真实证据验证、草稿资产上传后读回核验及不可覆盖标签；17项2.704秒隔离测试与actionlint退出0。本轮没有正式签名、设备验收或稳定发行，不能用合成证据包放行。原站逐字段对照整理主动差异，并纠正旧手机会员截图实际为缩窄桌面页，相关原站研究缺口保持。

- Telegram旧消息复核完成短期持久领取、版本绑定、同事务应用摘要及提交后确认；进程重启、应答丢失、旧租约、编辑竞争和回滚均有真实临时PG反例。复审发现旧采集器可忽略新claim，已补必填严格协议标识；缺失、错误及类型混淆在领取前422，完整事件与检查点不变，采集端不降级旧协议。最终冻结 `git diff --check && sh .runtime/telegram-reconciliation-20261027/run.sh` 107项73.901秒、零失败/跳过、退出0；日志SHA256=dca23456e6b3a77f825211a1f9d4ae9e4dcffc9941d2017935c0788f2c335e89。019生产迁移与同源工作器统一部署另验，不能以隔离成功冒充已上线。

- 重复完场快照原先对已关闭盘口继续生成 PostgreSQL 新行和 WAL，已改为仅关闭未关闭记录。冻结专项 `sh .runtime/final-acceptance-20261027/focused.sh` 退出0：媒体4项、阅读7项、匹配23项、独立盘口7项及原子快照10项，合计51项零失败/跳过；新增用例逐字段核对 xmin、ctid、源时间及接收时间未被重复采集改写。该优化减少无效写入，不把短窗低吞吐高等待宣称为磁盘瓶颈完全解决。

- 图片增加60秒同会话、上下文、消息版本、对象摘要与序号票据，逐次复核当前权益，读取后检查已有SHA256及长度；Nginx日志只含URI不含查询串，API关闭默认完整URL访问日志。消息/推荐/专家列表统一签发，管理员媒体保持独立身份。定向命令 `sh .runtime/final-acceptance-20261027/focused.sh` 退出0：媒体4项1.320秒、原文7项9.561秒、MCP23项28.153秒，零跳过；一次纯离线入口数据库项跳过日志保留、不计完整成功。详见PRIVATE_MEDIA_GRANTS验收文档。
- 旧混合大小写及空白别名检索不一致已修复；同队双赛、105候选和女足/青年队反例通过。PostgreSQL lower与Python casefold的非ASCII特殊字母差异明确披露，没有声称任意语言转写或人工确认别名已完成。
- 20:26:08 UTC远端22.010秒采样：CPU忙碌3.291%、iowait56.891%，整盘读0.3908MiB/s、写0.2453MiB/s、81.871 IOPS、io_time76.319%。旧业务停止后CPU与吞吐明显下降但低吞吐高等待仍存在；不能说磁盘问题已解决。20:24:47只读数据库无vacuum任务，8空闲连接与1空闲事务；累计temp9.21TB不是当前窗口写入，未据此改变数据库参数。
- 远端只读预检退出0：项目目录存在，运行目录尚无；Docker29.9.0/Compose5.6.0，SSH支持已核验Unix及TCP转发且GatewayPorts关闭。远端部署包继续实现，禁止双collector/executor并行，不输出或重造旧秘密。

## E103 本轮真实模型、Telegram 与完整功能复验（2026-10-28）

- 当前目标：复用旧项目 LLM 配置核验 Hermes 真实文字及图片；确认多个群组同时监控、新增来源自动同步管理员、现有信任作者昵称刷新、普通成员搜索并监控；复查比赛状态切换，随后完整功能/浏览器回归并按实际失败优化。
- 活动工作包：模型真实联调、Telegram 目录与身份同步、赛事筛选、遗留验收脚本和全套回归。状态为研究中。已有实现不能直接当作本轮真实联调通过。
- 初始证据：`git status --short && git branch --show-current && git log -5 --oneline` 退出0，当前分支 fix/TASK-78-hermes-telegram-sync、HEAD 26b6ef9；保留12个已有修改及未跟踪的远端tmpfs合同脚本，未重置他人代码。`docker ps`退出0，API/Web/Hermes healthy，remote-link unhealthy；本机collector/executor仍运行，先查拓扑再决定切换。
- 设计：管理员按钮保留原接口和角色过滤；普通成员只通过独立成员搜索选择，添加信任仍须管理员明确操作；昵称按稳定作者ID同步并保存历史，不自动授信。比赛切换必须清除过期行、拒绝迟到响应；真实模型只从受保护配置读取秘密，不输出密钥或私人消息。
- 验收：多群独立推进及单群失败隔离、完整管理员分页、新增来源触发、现有专家昵称/别名、普通成员搜索与监控、五种比赛阶段快速切换/失败重试；完整Docker功能回归、桌面/手机实际点击与错误检查。
- 失败处理及依赖：保留原失败码和日志，定位后重跑原检查。真实Telegram权限限制、生产HTTP/正式HTTPS、受控Android设备、官方赛果、人工语义金标及正式签名缺证须分别登记，不用模拟成功替代。下一步先做运行状态只读探针与冻结测试。
- 本轮阶段更新（2026-10-28）：Docker 运行 Hermes 真实结构化图片请求，框架 `hermes-agent`、实际回答正确识别左右红蓝区域，实际工具仅 `skill_view`，退出0；保密范围仅记录成功与模型标识，不写密钥或图片原文。只读API探针确认 Telegram 当前连接授权、4 个启用群组及活跃检查租约，2 个来源近期目录成功、2 个来源明确返回 `member_enumeration_restricted`；已有 4 条昵称历史，专家共 7、无ID占位昵称。真实读取普通成员仅在可枚举来源中返回管理员；普通搜索/成员信任的行为由隔离API和浏览器覆盖，不能声称真实普通成员已在群内命中。Docker `sh tests/browser/check.sh` 退出0，候选桌面/手机响应式交互回归全绿。发现离线筛选提示与清空旧阶段结果不一致，已更正文案并增加断网后切换阶段反例；等待定向重跑。
- 本轮优化及冻结验收（2026-10-28）：`sh tests/browser/check.sh` 在离线提示修复后退出0；`sh infra/test/run.sh` 冻结后端 949 项、零失败/错误/跳过，退出0，证据 `.runtime/test-evidence/3596995`。`sh tests/browser/accessibility-check.sh`、`sh tests/browser/model-configuration-check.sh`、`sh tests/browser/visual-check.sh`、`sh tests/browser/match-markets-check.sh` 均退出0；14 屏视觉基线未回归，比赛市场分页在桌面/手机遍历1207条并覆盖503重试/迟到隔离。`sh infra/remote/check-compose.sh` 与实际Docker tmpfs创建写入合同 `sh infra/remote/check-create.sh` 均退出0；临时Compose假配置拒绝真实秘密，未启动业务。
- 真实 Chromium 已对部署网页在1440及390视口点击全部五种赛事状态、日期/详情、专家、6个管理分区、4个TG来源管理员接口和普通成员搜索端点，10组工作流通过、浏览器JS错误0、页面横溢0，退出0；会员目录实际目前展示的为7条可见成员且两来源被上游成员枚举限制，未伪称真实普通用户命中或全群组目录可用。Hermes实际模型图片请求HTTP200、4.571秒，使用技能工具一次，准确读出红/蓝两色；配置秘密未进入本报告。浏览器与真实模型的证据分别保存在忽略目录 `.runtime/acceptance-20261028/`。
- 例外/安全事件：在只读排查旧验收产物时，完整编排输出包含模型提供方凭据；已立即把该单文件限制为仅属主可读写，停止再次读取或传播。仅报告类别，不记录值。本地受保护配置尚未轮换；如需轮换，应通过既有保密配置流程安排，当前真实模型请求仍成功。过去的只读探针显示远端 worker 尚未切换、本机 `remote-link` unhealthy，因此不能记录远端采集接管完成；切换维持外部依赖受阻，继续验证链路根因，不因网页/模型通过改写部署状态。
- 收尾阶段（2026-10-28）：Android Docker 候选构建退出0（90项 Gradle action，2分25秒；开发产物，不是已签名正式包或设备安装证据）。`sh tests/integration/run_hermes_acceptance.sh` 首次退出1，失败发生在测试开始前：本机缺少脚本摘要锁定的验证镜像 `sha256:d42d2ec203d3b9d31df31bb36b05e346c426ebc701bbf39392b1b824776a9dbd`；错误日志保存在 `.runtime/acceptance-20261028/hermes-isolated.log`，准备拉取相同摘要后原命令重跑。远端仍只有 bridge/socket-guard，无 collector/executor；本机 collector/executor 各运行一个，remote-link unhealthy。远端控制 Unix API 只读请求 HTTP200、配置版本9；这些事实尚未证明租约交接、镜像清单和全部切换门禁，因此没有停止本机工作器或启动远端工作器。
- 根因修复与下一阶段（2026-10-28）：旧Hermes验证器写死的是本地镜像ID，没有可拉取的仓库摘要；改为冻结Dockerfile及三服务依赖清单，以项目既有固定基础镜像构建任务独有验证器并记录内容ID，结束清理该任务标签。`bash tests/integration/run_hermes_acceptance.sh` 原样重跑退出0，证据 `.runtime/af-hermes-framework-3642540`：正常/注入攻击/截断/重启恢复/四轮预算全部通过，图片与技能输入、260候选13连续页及服务端工具约束正确；提供方为显式替身，不冒称第二次真实模型成功。临时数据库两轮浏览器联调退出0、交易调用0；Android上述构建包含单元测试及Lint，尚无本轮设备安装证据。
- 远端切换进入真实操作前阶段：两次只读门禁返回远端worker各0、保护环境0600、socket目录及对象归属权限正确；远端collector/executor实际拉取镜像的摘要、ID及OCI源码标签均对应26b6ef9，桥配置版本9。早期把只读根文件系统当作可写tmp导致两个工具入口失败，未改容器权限或扩大挂载；改用Docker内存参数传入已有SSH只读工具后退出0。下一步保持API/执行器实盘false，先停本机两worker、读取租约及未知订单、确认四类实例均0，再生成真实观察清单通过门禁后启动远端，任一前置失败按原清单恢复本机；不将remote-link尚无远端监听端时的unhealthy单独认定为永久外部阻塞。
- 切换预检补证：远端限定 `v2/` 前缀的S3 HTTPS HEAD/list、原CA校验、签名可读、执行UID10004及实盘false均退出0，write_performed=false；本机只读数据库确认4个采集租约、账号任务0、续期0、订单0，API与执行实盘false。overlay首次渲染因未传锁定的隧道镜像变量退出1，尚未停止服务；添加仅含非秘密摘要的独立保护环境文件、保留原运行配置，随后双env渲染退出0。验证器语法在Docker内检查退出0。本阶段代码和证据准备提交，远端实际交接仍待执行，不提前勾选。
- 可审查验证阶段提交 `4619390ab02576c4a89115e61aae567bb707bc4a` 已推送任务分支，`git ls-remote`核对相同完整SHA，退出0。00:13:48 UTC本机collector/executor按清单停止，命令退出0；确认两类本机实例0，API及网页继续运行。停止后的4个旧租约最晚00:15:11.237396 UTC自然到期；未清空检查点、撤销会话或强改租约，远端worker尚未启动。接下来重新读取排空结果和四类实例，生成门禁清单后交接。

- 远端实际交接（2026-10-28 00:15—00:20 UTC）：租约自然排空后重新验证部署门禁 `eligible=true`、无阻断项；远端collector与executor各1、本机对应worker为0，executor/API实盘开关均false。随后以 `runtime.env` 与独立非秘密镜像摘要overlay重建API，API与remote-link均healthy且零重启。00:19:54 UTC只读数据库探针确认4个来源租约由同一新collector持有、四个消息检查点均高于停机前值；运行账号任务0、续期任务0、订单0。Telegram控制配置消费版本9。证据保存在忽略目录 `.runtime/acceptance-20261028/`；本次证明单实例接管和检查点继续推进，不证明所有Telegram上游权限、普通成员可枚举或乐鱼/模型业务持续健康。
- 用户新增 App 重构要求：以旧项目 CC 浏览器分析结果、静态资源和布局设计为构建依据，并且不将 HTTPS 校验作为应用可运行的前置条件。当前先定位旧项目原始分析、资源来源/授权和现有实现，再确定可复用项及差异；不把捷报参考研究冒充 CC 结果。HTTP页面部署与HTTPS证书校验是不同情形，需在设计记录中说明实际支持行为。此工作包仍为研究中，尚无代码完成或浏览器验收证据。

## E104 CC 参考 App 重构与 HTTP 配对（2026-10-28）

- 当前目标/阶段：旧研究核实结束，设计已记录、代码已写，准备冻结验证。责任范围网页/Android/客户端测试/报告；继续现行专家业务，不引入旧算法与彩票业务。
- 来源证据：Docker只读读取指定旧备份 CC 分析，确认 APK io.orinn.browser 5.5.2/2552、Flutter/InAppWebView，旧截图为离线重建，几何尺寸并非运行实测。已提取并逐张查看通用公告/VS 图标，含竞彩品牌的背景及热门资讯文案图从本轮运行资源中排除。来源、SHA256、未核实授权和替代方案见 reports/cc-browser-reference/REPORT.md。
- 设计/实现：蓝色固定顶栏、1240px桌面主体与紧凑赛事/右栏、手机固定底栏；Android原生壳同步蓝色。显式HTTP/HTTPS配对、各自默认端口、同源隔离、无重定向服务身份探测；HTTP不需要证书，HTTPS保持系统证书校验。二维码、帮助与设备脚本同步，新增HTTP真实本地服务及错误响应/危险scheme反例。
- 实际操作：`git status --short` 初始仅本轮进度修改；Docker提取图片退出0。文本/资源编辑已完成；一次只读检查引用不存在的colors.xml和acceptance.mjs退出1/2，随后定位实际styles.xml及verify.mjs，未更改业务以适配错误路径。
- 验收/失败处理：冻结后完整后端、Android单元/Lint/双包构建、桌面/手机浏览器、可访问性和视觉复核；预期视觉变化须人工查看后更新基线。随后仅更新项目Web服务，重复实际部署点击。保留失败码，修根因重跑；无受控Android设备/许可证明/真实普通成员目录权限的缺口分别记录，不能预填完成。下一步代码冻结并执行Docker验证。
- 冻结验证阶段（2026-10-28 00:39 UTC）：`bash android/build-candidate.sh` 退出0，90项Gradle action、2分31秒，单元/Lint/debug及unsigned release已生成；这不是正式签名或设备安装证据。`sh tests/browser/check.sh` 与 `sh tests/browser/accessibility-check.sh` 均退出0，证据分别为 `.runtime/browser-ui-3702110` 与 `.runtime/accessibility-ui-3704598`，PNG资源加载、桌面/手机交互及已确认无障碍规则通过。`sh tests/browser/visual-check.sh` 首轮退出1，14屏与旧紫色基线存在预期设计差异，保留 `.runtime/visual-ui-3708697`；正在逐屏人工复核，未放宽像素阈值。完整后端仍运行，设备就绪独立检查中，下一步仅在视觉复核后更新基线并重跑原检查。
- 视觉/设备复核（2026-10-28 00:43 UTC）：人工查看1440与390两张联系表中的14屏，蓝色头栏、密集赛事、VS、会员/个人/管理与详情无裁切或遮挡，允许按显式reviewed流程更新基线；像素阈值维持通道16、变化比例0.002。第一次更新误用Docker network none使浏览器离线，赛事未加载退出1；改为与原入口相同的项目internal网络后14屏基线生成退出0，当前原比较入口重跑中。联系表生成最初漏stdin、继而遇到非root写入权限，均只影响临时证据，修正限定任务挂载后生成成功。`sh infra/android/check-device.sh` 退出2：授权设备0、无KVM；仍缺新包安装/真实WebView配对证据。Android XML核对28项（策略12、导航6、HTTP/TLS探针10）零失败/错误/跳过。模型配置UI与1207盘口分页双端复测均退出0。
- 后端全套首次命令在结果汇总前被SIGTERM中断（进程退出143、无最终报告），没有将中间点号当作成功；原始日志保留backend.log。现以独立Docker编排启动同一 `sh infra/test/run.sh` 并持久记录最终退出码，未改测试或放宽资源/断言。网页真实API临时库验收同时启动，冻结产品源码保持不变。下一步等待最终结果，再提交/部署Web并执行真实点击。
- 专项收尾（2026-10-28）：视觉原入口重跑退出0，14屏均满足原阈值，证据 `.runtime/visual-ui-3721549/visual-results.json`；真实API临时库 `sh tests/browser/integration-check.sh` 退出0，17原场景、身份矩阵与8项账户流程通过，核验/刷新/回执各1、交易调用0、失败0，证据 `.runtime/web-integration-3721988`。当前没有修改冻结产品源码；等待完整后端最终报告后，按已授权范围仅更新本项目Web并做真实HTTP双端点击，不重启已在远端运行的采集/执行工作器。
- 阶段提交：`81a324fb43240d26841caff54f86ac557c4aea87` 已提交并推送当前任务分支，`git ls-remote` 完整SHA一致，退出0；提交仅本轮40个代码/测试/基线/报告文件。`git diff --cached --check` 退出0，新增差异和报告的常见凭据模式扫描零命中（有限模式检查，不等于全面安全审计）。首次被中断的本任务测试PG与internal网络已按具名清理，当前重跑容器保持。下一步为该提交构建候选Web，完整回归通过后仅替换Web并读取真实接口验证。
- 完整回归结束：原入口 `sh infra/test/run.sh` 重跑退出0，951项、393.143秒，零失败/错误/跳过，证据 `.runtime/test-evidence/3721525/backend-tests.json`；本轮报告如实记录父提交4619390和committed_source_verified=false，产品/测试冻结摘要将另核对当前提交，不伪称干净发行证据。81a324f已触发精确分支workflow_dispatch；候选Web `2.0.0-dev.cc.20261028.1` 构建退出0、OCI revision=81a324f，镜像清单摘要 `sha256:cedac221d5d61da4f5b17db15dd4ab247dfe7d7af9bcc3db98dabd8b852c1f2c`。下一步仅更新项目Web并运行真实API点击。
- 部署及证据核对：Web单服务替换退出0；565份冻结文件摘要复核退出0，只有进度与CC报告两份文档不同，产品/测试完全一致；机器报告含加载总耗时395.326秒。首个浏览器容器误用连字符网络名退出125，改查实际下划线网络后重跑；摘要读取首次非root无权限退出1，限定只读/root复核成功，未改文件权限或产品逻辑。

### E104.1 回归后的账户刷新竞态优化

- 目标/责任：网页账户状态异步加载及真实API浏览器验收；前置CC开发候选已部署，951项/28项及各网页专项完成。状态研究中。
- 当前证据/缺口：复查上轮4619390的GitHub运行40607356281，Android/后端成功，账户最后场景填daily_limit超时；真实GET虽已到达，账户导航与手动刷新并发会迟到重置enabled并禁用正在填写的字段。本地此前成功不消除该竞态。
- 设计/动作：账户读取增加代次，旧响应不再覆盖新表单；读取期间明确busy并暂禁编辑/保存，独立紧急停止不禁用。用真实临时API延迟旧GET，验证第二次刷新后编辑不会被旧快照覆盖，保留会员撤销403与停止200原断言。
- 验收/失败处理：先重放确定性迟到响应，修复后原完整API浏览器入口及其余网页门禁重跑；独立提交再构建候选Web。失败保留原日志与退出码，不增加固定等待掩盖竞态、不删除权限断言。GitHub已授权修复使用gh-fix-ci排查方法，不因技能通用审批建议再次阻塞用户已明确授权的修复。
- 设计已记录/代码已写，现冻结测试：旧GET保持在真实API响应之后，第二GET先完成；期间断言busy与编辑禁用、紧急停止可用，恢复旧回包后断言新enabled和限额不被覆盖。旧源码副本与修复源码分别执行完整入口，证据隔离于 `.runtime/account-race-20261028/`。一次gh日志读取因远端全流程仍运行返回1，未据此认定业务故障。81a324f部署HTTP网页12组（1440/390各6组）真实点击全部通过、退出0，证据 `.runtime/cc-app-20261028/live-clicks.json`；该成功不代替新竞态修复的验证。
- 第一轮账户修复 `sh tests/browser/integration-check.sh` 退出0，冻结 `.runtime/web-integration-3755741`：17+8场景、迟到GET、会员撤销403/停止200及交易调用0均通过。旧副本反例退出1，但失败清理先unroute导致待释放请求Route已处理异常，不能作为有效产品反例；已修为等待处理完成再解除路由。远端40611386478完成：后端/Android成功，浏览器在会员授予时automatic_betting为undefined；不同于此前账户限额失败，保留两个失败。进一步确认管理员初始载入与重新进入可能重叠，套餐快照覆盖刚勾选权益。新增管理载入/套餐代次、加载期禁用，加入双GET反序后勾选保留及PUT请求/回包权益断言，准备新冻结重跑。
- 第二轮退出1，冻结 `.runtime/web-integration-3769615`：套餐迟到响应与勾选保留已通过，但新增测试误认为PUT会返回entitlements；实际合同仅id/code/enabled。改为PUT请求断言加独立GET持久化读回，未改后端响应适配测试。旧源码反例 `.runtime/account-race-20261028/counterexample/` 正确退出1：加载时aria-busy缺失，清理无异常。补齐套餐重载时保留选择、授予按钮暂停及保存等待重载，现再次冻结执行原入口与所有网页专项。
- 最终隔离验证通过：`sh tests/browser/integration-check.sh` 退出0、冻结 `.runtime/web-integration-3781000`，17+8原场景与两处确定性迟到响应全部通过；替身核验/刷新/回执各1、真实交易0、失败0。`sh tests/browser/check.sh`、`accessibility-check.sh`、`model-configuration-check.sh`、`visual-check.sh`、`match-markets-check.sh` 均退出0，证据分别为3781233、3784408、3786896、3787428、3788763；14屏维持原视觉阈值，双端1207盘口分页无回归。`git diff --check`退出0。准备独立提交/推送优化、执行精确源码远端CI及仅Web候选更新，再做真实点击；Android和Python源码未在本轮优化中修改。
- 优化阶段提交 `771faf32862db30b0bab6c99c992267138c7e294` 已推送，远端完整SHA一致、退出0；有限新增差异常见凭据模式扫描零命中、暂存差异检查退出0。Web候选 `2.0.0-dev.cc.20261028.2` 构建与单服务替换均退出0，源码标签771faf3，镜像清单 `sha256:f00b4896e1f8fb33e5675cc8d6327d4e2ac2f4400714ee4d31525ae31535f275`；未操作API、远端采集/执行或原有数据。精确分支CI40612966677等待结果，部署后双端真实点击已启动；下一步读取结果，不预填通过。
- 真实联调通过：优化后开发候选1440/390共12组实际HTTP点击全部成功、退出0，JS错误/横溢均0，证据 `.runtime/account-race-20261028/live-clicks.json`；包括五状态、日期/详情、专家、管理员六分区、四群来源读取/普通成员搜索入口及会员/个人。容器内app/admin脚本SHA256与771faf3逐项相同，Web/API/remote-link/Hermes存活健康；该读数不扩大为上游持续健康结论。两处竞态的写操作只在临时数据库验证，生产未写合成业务数据。远端CI仍运行，下一步完成证据归档及分支同步。
- Android远端精确源码构建成功：已下载CI40612966677的开发产物，Docker逐项核对35份Android/测试源摘要与771faf3一致、工作树状态空，28项零失败/错误/跳过。首次非root只读验证无目录访问权退出1，未改权限；限定root只读验证退出0，证据 `.runtime/account-race-20261028/ci-android-verification.json`。独立交付目录 `android/build/artifacts/cc-20261028-771faf3/` 保留原产物，debug SHA256=0b6681ef7d1a987f1c1b5f698b8709f5eb818623a652ed094a4038b3b5444393，unsigned SHA256=ee5f0bafaa813c8d8e1e60bf933b4fdfc9a1127c772df669442db1c6a0bab095；仍为Debug身份及未签名候选，无设备安装证据。
- 远端完整回归结束：精确源码771faf3的CI40612966677三个任务全部success；后端951项266.044秒、零失败/错误/跳过，浏览器含两处迟到反例与全部原门禁，Android28项/Lint/双包通过。机器状态与原日志保存在 `.runtime/account-race-20261028/final-ci.json`、`final-ci.log`，原始命令均退出0。根进度摘要同步当前TG授权、远端位置与独立执行要求，移除把早期状态写作当前事实的文字。下一步仅提交/推送本轮证据文档并核对远端；业务源码继续对应771faf3，未声明生产完整交付。
- 收尾缺口：受控Android设备/可用加速环境、正式签名及升级验收、CC两图标许可、两个TG来源成员枚举权限、真实普通成员命中、官方赛果及人工语义金标仍各自缺证；模型凭据曾出现在受保护排查产物，已限制文件访问但尚未轮换。普通成员机制的隔离通过不能冒充全群真实可枚举，模型图片成功不能冒充业务语义金标，开发包不能冒充正式发行。

## E105 Hermes 实际运行闭环只读审计（2026-10-28）

- 目标/责任：解释“模型接口已通”与“TG 图文已经产出可用专家推荐”的差异；只读取运行中审核工作器的聚合状态、轨迹类型和失败类别，不读取 Telegram 原文、模型正文、来源标识、媒体或凭据。状态为研究中。
- 实际命令：在 `ai-football-v2-review-worker-1` 容器内使用其受保护 `DATABASE_URL` 开启只读事务，保存 `.runtime/account-race-20261028/model-runtime-audit.json` 与 `model-runtime-audit-detail.json`；两个聚合查询退出0，未改变任务、配置、租约或数据。
- 运行快照（查询时刻最近24小时）：996 条 queued、3 条 running、590 条 unresolved；unresolved 中有590条 Hermes结果、602次请求、839次搜索、143次消歧、24次盘口读取、5次记录调用，但系统回执244条全部 `accepted_count=0`。失败/重试另有8条 `lease_exhausted`、1条 `ReadTimeout`、27条 `source_edited`、3条 `ConnectError`、2条 `ReadTimeout`、10条 `TimeoutError`；这些是吞吐与任务生命周期缺口，不是模型接口不存在。
- 工具闭环证据：系统只接受已搜索、唯一消歧、读取当前有效盘口并由 MCP 记录的推荐；`review_summary.py` 明确不把模型自由文字或 Telegram 原始赔率当作成功。当前绝大多数审核没有形成完整的工具确认链，记录调用5次且每次均为0接受、1拒绝，因此最终业务结果为 unresolved。
- 质量证据边界：旧项目配置的真实 Hermes 视觉请求曾返回HTTP200并正确识别合成红/蓝色块，但该证据只证明路由、原生图片传输和技能可用。现有框架验收使用确定性替身和1×1 PNG；质量回归全为 `synthetic=true`，尚无真实 TG 图文人工标注集、真实体育截图 OCR/语义精确绑定和可复核的模型比较报告。
- 根因结论：功能尚未做好是三个缺口叠加——审核队列积压、模型到赛事/盘口工具的闭环完成率低、真实语义质量未被金标验证；不能用“HTTP200/模型已消费配置/页面点击通过”替代这三项证据。此前把视觉传输和框架验收描述得接近模型功能完成，表述不准确。
- 下一步与验收：先排查并恢复队列吞吐和超时/租约重试，再抽取经授权的真实 TG 文图建立人工标注集，执行真实模型回归，逐条核对搜索分页、唯一比赛、盘口报价和记录结果；只有真实报告、人工复核及生产样本链路均通过，才能把本工作包从研究中改为真实联调通过。

## E106 Hermes 真实分析修复（2026-10-28）

- 当前目标/责任：修复专家图文审核的可用性，范围为审核队列、Hermes工具及专家消息阅读；前置依赖是现有旧模型配置、TG信任作者及乐鱼赛事与报价。状态：本轮功能链真实联调通过；语义金标及长期质量尚未验收。
- 初始证据：`git status --short --branch && git log -3 --oneline` 与 `docker ps` 退出0，工作树干净，API、审核工作器、Hermes及MCP正在运行。Docker内只读抽查发现当前大量消息为闲聊、推广或历史档案，未调用盘口工具可能是合理行为；E105的工具次数不能单独证明模型故障。实际5项工具拒绝中4项为源报价不新鲜/状态不可用、1项为盘口变化。历史消息禁止绑定当前报价的业务约束保持。
- 缺口与行动：逐项复核实时消息识别、工具契约、赛事/盘口新鲜度、模型结果展示及历史任务调度，依据复现修复确定缺陷；建立隔离真实模型图文与工具链验收，避免向运行库写测试消息或改变原始数据。
- 验收与失败处理：冻结代码后使用Docker临时数据库跑正常、无推荐、历史、过期及多项推荐反例；真实Hermes配置在隔离环境完成文本/图片与足球工具联调，再验证运行服务和浏览器。真实赛事/报价不存在时记录实际限制，不制造推荐或把历史报价替换为当前报价。下一步确认隔离联调的部署配置和真实盘口源状态。
- 已定位并修复两处确定缺陷，待冻结测试：远端 PostgreSQL 实测单次往返约0.23秒，审核调度的多个顺序往返被5秒整体截止反复取消；改为30秒整体截止，原3秒单语句和2秒锁等待限制保持。消息详情原来按时间正序每次只加载30条，最新模型结果可能被大量工具轨迹挡在后页；新增当前任务当前尝试的最新模型结果直达展示，历史尝试不冒充当前结果，原始轨迹分页和系统结果分离保持。Docker构建验证镜像退出0，`git diff --check`退出0；下一步运行含超时、旧尝试和浏览器转义的定向回归。
- 隔离真实模型验收（Docker；旧项目受保护配置只复制到权限600的本任务文件，端点与密钥未写入输出）：专属Hermes v0.22.5接真实旧模型提供方，专属API/MCP及临时PostgreSQL应用18项迁移。实际命令为 `docker run --rm --network ai-football-hermes-fix-test --cpus=.75 --memory=512m -e INTEGRATION_DATABASE_URL=postgresql://postgres@ai-football-v2-pgverify:5432/v2_integration_test -e HERMES_TEST_URL=http://hermes-real:8642 -e INTEGRATION_SCENARIOS=text,image,multiple,none,stale ... python tests/integration/hermes_real_model.py`，最终退出0。首次退出1的原因是测试错误要求模型对已识别的过期报价再调用记录工具；实际模型读取盘口后选择不提交，属于正确安全结果。按“过期报价已读、无接受推荐；若提交则必须由系统拒绝”修正断言后冻结重跑，5场景全部完成：文本接受1、图片接受1、多项推荐接受2、闲聊接受0、过期盘口接受0；每项接受均含搜索、消歧、读盘和成功工具证据，合成赛事及数据仅在临时库。摘要 `.runtime/hermes-real-20261028/real-model-results.json` SHA256 `da11919d4ca55b2bfebe5808f5e6daeceecdd9760b06edde211b38a0cc40d956`；测试源码 SHA256 `a6c234ac30ca2b55a48b0e76c2453aa68c1d56cdaae24739fcb6cc7d567e9d87`。专用容器、临时数据库、网络及凭据副本已清理。该结果证明当前模型到受限工具的功能链路，不构成真实Telegram语义金标或长期质量验收。
- 阶段提交：`0ac3603`（Hermes调度及最新分析展示修复）已推送当前任务分支；本提交代码与上述模型验收源码相同。下一步以干净提交执行完整冻结`sh infra/test/run.sh`、Web专项和真实浏览器点击；再复核运行中API/工作器镜像及队列推进，生成带提交标识候选并部署。进度记录命令、退出码、阶段提交与远端引用须在收尾补齐。
- 最终冻结验证：`sh infra/test/run.sh` 退出0，953项、零失败/错误/跳过，unittest381.525秒、机器报告含加载383.702秒，`.runtime/test-evidence/152002/backend-tests.json` 标记 `committed_source_verified=true`、源码e5cc40d。`sh tests/browser/check.sh` 31项退出0（`.runtime/browser-ui-174905/results.json`）；`sh tests/browser/integration-check.sh` 原17场景与扩展8场景均通过、退出0（`.runtime/web-integration-176923`），核验/刷新/回执各1、真实交易0；`sh tests/browser/accessibility-check.sh` 双端零已确认违规、退出0（`.runtime/accessibility-ui-184620/results.json`）。`sh infra/check-runtime.sh` 和差异空白检查退出0。
- 候选部署：按源码e5cc40d构建API与Web候选 `2.0.0-dev.hermes.20261028.1`，构建、API→审核工作器→Web顺序重建均退出0；API镜像清单SHA256 `f0468d8bfb32f6906a98043d9389d724971613c48499d37a413cd8ee0b42079e`、Web `c02d89d815bd6b942101e9232444c2b967c4220eb47e0a9d9b238a8baba5fee8`。初次API重建遗漏现有 `infra/remote/compose.local.yml`，部署复核发现控制端指向已停用本机collector；最终Compose明确加入该覆盖层，真实控制端状态HTTP200、authorized=true、connected=true，API readyz200且healthy。未重启远端采集或执行器，未做迁移或写测试业务数据。
- 部署真实点击：Docker Chromium 1440/390两宽共14组全部成功、退出0，零页面JS错误；实际打开现有TG消息，页面最新分析329字符与API当前尝试逐字一致；未输出原文内容。五种赛事状态、日期详情、专家、管理六分区、四群目录、普通成员搜索入口及会员/个人页通过；两来源仍如实报告成员枚举受限。产物 `output/playwright/hermes-20261028/live-clicks.json`，截图已目视检查手机比分布局。API/审核工作器worker与阅读模块、Web app.js逐份SHA256均与冻结源码一致。
- 运行证据：06:29:40 UTC只读 `.runtime/hermes-real-20261028/runtime-final.json` 退出0，最近20分钟59次模型结果及59个已完成审核，最新结果06:29:32、完成06:29:34；四启用来源全部5分钟内检查点推进，7位启用专家无编号占位，当前过期运行租约0。新工作器日志未见调度超时或审核失败；旧历史queued/retry仍由有界队列继续处理，不删除积压、不伪造当前推荐。下一步仅归档本轮证据、推送并核对远端引用；真实TG人工金标和正式设备/签名仍独立未验。

## E107 清理历史审核积压并恢复实时专家处理（2026-10-28）

- 当前目标：响应用户明确授权，停止处理积压的 `historical_replay` 队列，使实时专家消息立即优先。采用可审计状态转换，不删除Telegram原文、媒体、既有轨迹、会员、订单或其他业务记录。
- 前置只读证据：容器 `ai-football-v2-review-worker-1` 内受保护数据库连接；只输出计数和时间。检查时间2026-10-28 10:56 UTC，历史回放queued 422、retry 3；实时queued 4、failed 11、unresolved 175。最近15分钟Telegram事件26条、消息2条；四来源检查点均在5分钟内更新；当前任务租约有效。数据库均为远端正式数据服务，用户已明确授权清理历史积压和恢复处理。
- 设计及动作：在公平调度advisory锁和短事务内精确选择历史queued/retry任务，将状态置为unresolved、原因写为人工清理，并为每条追加操作轨迹。原始Telegram消息、图片对象、既有轨迹保留。第一批423条已完成转换。随后确认仍有1条历史running占住同专家并发额度，直接阻塞2条实时队列；为恢复处理，将在调度锁下使该历史租约失效并追加轨迹，实时任务本身和其他历史终态不触碰。模型/工具租约校验负责拒绝失效任务后续写入。
- 效率缺口与动作：最近60分钟165个模型返回的输入tokens均值12114、输出529，模型处理p50 11.3秒/p95 16.2秒；Telegram同步在清理后继续每分钟将旧2021/2023/2025原文创建新历史任务。入口修复按现有24小时真实发布时间窗口决定自动调度：旧原文/图片仍持久保存，但建立带人工操作原因的unresolved任务和轨迹，不调用LLM；当前及近24小时消息保持queued实时审核，未来时间戳不会误作实时；显式管理员重审路径保留。该优化优先去掉423条积压的无收益LLM调用，不提高共享网关并发、不改变模型提示及工具规则。
- 验收记录：`docker run --rm --network ai-football-v2-test-net --cpus=.75 --memory=512m --pids-limit=128 --read-only --cap-drop=ALL --security-opt=no-new-privileges --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m -e PYTHONDONTWRITEBYTECODE=1 -e TEST_DATABASE_ADMIN_URL=postgresql://postgres@ai-football-v2-pgverify:5432/postgres -e TELEGRAM_TEST_DATABASE_URL=postgresql://postgres@ai-football-v2-pgverify:5432/postgres -v /root/ai_football:/source:ro -w /source/tests -e PYTHONPATH=/source/services/api:/source/tests:/source ai-football-verifier:hermes-fix python -m unittest test_telegram_api test_review_freshness_scheduler test_review_worker -v` 退出0，59项通过、0失败、0跳过。第一次试跑因漏设仓库根`PYTHONPATH`导致1项导入错误；正式配置重跑已通过。真实运行API `/healthz`、`/readyz`均200；当前分析活动队列0，worker正在运行的历史并发环境值为默认1、实时并发2。Docker Compose 首次只读渲染因未传必需的`REMOTE_TUNNEL_IMAGE`退出1，已从运行容器镜像RepoDigest确认正确固定值为`sha256:e754c9f1e70611589051b137d4ae928aafe77f0f88913031895dbebf6611cb40`，后续命令显式传入。
- 全量Docker冻结测试首轮：`sh infra/test/run.sh` 在955项中954通过，唯一失败是对账测试使用固定模拟编辑时间早于本轮动态测试消息。将模拟编辑时间绑定到数据库事件版本后，专项重跑又暴露该编辑消息按实际时间窗应为历史终态而非queued；断言修正为`unresolved/historical_replay`，原失败用例Docker隔离重跑通过。
- 全量Docker冻结测试修复后：`sh infra/test/run.sh` 退出0，955项、0失败、0错误、0跳过，用时393.752秒；证据 `.runtime/test-evidence/752956/backend-tests.json`，源码提交`9da9b809d51236f9ead4c6606aaab94799b39435`，`committed_source_verified=true`。恢复演练涉及45张表、1个媒体对象，前后业务盘点差异为空。专项59项和历史队列状态检查均通过；此时线上仍运行旧镜像，未更新API/worker。
- 实时部署联调：API候选`2.0.0-dev.hermes.20261028.2`已本地构建，revision `debf636`；API ready/health均200，远端Telegram控制status为connected及authorized。worker以`REVIEW_HISTORICAL_CONCURRENCY=0`和实时并发2启动；来源检查点4/4仍有效且5分钟内刷新。API启动后6条Telegram对账事件中3条为旧历史消息，状态均为unresolved，3条抑制轨迹，无历史queued/retry/running；没有新实时专家消息，因此本观察窗口内实时LLM完成数为0，不能标记真实实时模型联调通过。
- LLM往返效率审计：候选搜索MCP接口上限100，但MCP工具和API默认均为50。将模型默认搜索页提升至100，保留cursor、不可变全候选快照及“翻到next_cursor为空才可消歧”的约束。105个隔离候选从默认50的3次搜索调用降到默认100的2次，减少33%分页往返；新增API完整尾页和MCP schema默认测试。最近60分钟生产追踪记录78个模型返回，但没有成功的候选搜索工具轨迹，故真实推荐场景节省仍未测得。
- 页大小专项Docker验证：`python -m unittest test_reviews test_mcp_transport -v` 退出0，29项通过；覆盖105个候选的100+5尾页、cursor完整结束和MCP工具schema默认100。效率对比样例为50默认下50+50+5三页，100默认下100+5两页，少一次模型工具往返。
- 页大小优化冻结回归：`sh infra/test/run.sh` 退出0，956项、0失败、0错误、0跳过，用时398.240秒；证据 `.runtime/test-evidence/791078/backend-tests.json`，提交源码 `e97d65f`，`committed_source_verified=true`。
- 最终运行验收：默认分页候选`2.0.0-dev.hermes.20261028.3`的API、Hermes MCP及review-worker均本地构建运行，OCI revision `5b02911`；Hermes使用既有固定镜像v0.22.5。全量冻结Docker套件956项退出0，API health/ready 200，Hermes health 200，MCP真实`tools/list`仅见四个白名单工具且`search_matches.page_size.default=100`。Hermes重启前后真实旧模型只读completion均返回正常并带usage；不执行业务MCP调用。worker环境历史并发0、实时并发2；数据库当前活动任务0、历史活动任务0；4/4 Telegram来源租约有效且最近5分钟刷新；Telegram控制状态connected及authorized，3个启用来源健康状态已返回。候选分页版本上线后的短观测窗有9条Telegram对账事件但均未形成新实时消息；之前队列恢复窗口的4条实时审核均终态unresolved而非虚构推荐，模型耗时p50 10.97秒、p95 16.00秒、平均输入11814 token、输出391 token；完整处理队列等待p50 32.25秒、p95 50.92秒，4项样本显示同一专家并发受限下的串行等待，未提高共享模型并发。样本只有4条且早于分页100版本，不能作为分页优化实测结论。
- 最终状态：历史自动回流已在入口抑制，历史worker lane关闭，任务原文、媒体及既有审计均保留；模型默认候选页由50提升到100并维持完整游标遍历，105候选隔离样例搜索往返由3降至2。423条历史任务按旧平均模型使用量估算约避免512万输入token、22.4万输出token和80模型分钟，为投影不是实测值。当前活动队列清空，模型及Telegram服务真实链路健康，任务分支已同步；实时Telegram事件到达时继续以真实模型结果监测100条分页带来的token、回合和耗时。真实语义质量、相同专家串行队列延迟及新分页配置的线上节省尚需更大真实样本，不据小样本宣称完成这些指标。

## E108 端到端审核低延迟（目标3秒，2026-10-28）

- 目标：将Telegram实时消息进入模型并形成系统结论的端到端时间压到3秒以内；这属于性能目标，外部模型调用存在非确定时延，未经真实样本不得宣称达到或承诺硬性SLA。
- 责任目录：`services/collector/`处理Telegram事件、群组锁及媒体阶段；`services/api/`审核队列和worker唤醒；增量迁移在`services/api/migrations/`；行为验收位于`tests/`；运行证据追加本记录。
- 前置证据：真实数据库最近2小时27条可关联实时审核，事件入库至模型结果p50 64.9秒、p95 844.5秒、最大2142秒；单次模型链路约9至17秒。同一专家全局并发上限为1。当前服务已有asyncio/httpx/Telethon异步I/O，网络等待不适合再包线程；图像解码、图片存储已使用受限线程池。
- 设计决策：由Telethon推送处理实时新消息；数据库事务提交后用无内容PostgreSQL NOTIFY唤醒空闲worker，监听断开时保留1秒有界轮询；持久化pending后立即续传媒体；不在同群锁里做目录枚举、群实体解析、消息/回复读取及媒体上传；每次消息写入仍由API验证租约，短本地缓存只减少额外检查点往返。模型调用、完整候选搜索、图片理解、盘口证据和安全校验均不截断、不绕过。
- 缺口与实现：旧路径的实时图片要等最多10秒周期重试；目录刷新与实时消息互斥；空闲worker每秒探队列；单条消息重复查询群实体、作者和检查点；相册图片串行从对象存储读取且重复解码。已加队列提交通知及退避重连、实时媒体立即上传、移出阻塞RPC、缓存作者与短期租约核验、最多两路图片准备并发和分阶段脱敏耗时。遥测分别记录采集回调、事件入库到领取、图片准备、模型提供方响应、模型结果和审核结论。历史lane仍关闭，专家和模型并发不扩。
- 验收场景：插入只发无敏感内容通知且提交后可领取；数据库断连监听失败时轮询仍工作；相册及图片先持久pending再立即传输且上传不占群锁；目录刷新慢时新事件仍在有限窗口内持久化；源版本、信任作者、回复、图片哈希及Hermes安全限制均通过原全套回归。故障不丢原文、不确认未完成媒体、不把模型自由文字当系统结论。
- 隔离定向验证：`python -m unittest test_review_worker test_telegram_collector test_telegram_reconciliation_claims.ReconciliationClaimsTests.test_migration_preserves_existing_event_and_reapplication_is_noop test_runtime_settings test_hermes_images -v` 在Docker中76项通过，退出0；覆盖通知只在事务提交后发出且无payload、实时图片入队和上传、慢目录锁、租约缓存过期刷新、缓存作者/群对象、最多两路并发图片准备、图片完整性及当前模型配置。
- 修复记录：首轮通知领取用例被夹具预置running任务占满作者额度；随后将夹具任务终结。迁移合同先只期望019，已改为验证019/020顺序和重放。Collector租约批量替换曾引入缩进错误，隔离导入发现并修复。结论延迟断言又发现领取阶段分段值没有随任务对象传给结论，已补上并通过原组合重跑。
- 隔离实现状态：代码已写；collector、worker、Hermes图片区段定向隔离验证通过；当前冻结完整套件待执行。历史自动审核仍关闭。运行基线模型约9至17秒，3秒整体目标现阶段未达到且不可能由worker线程/调度单独保证；真实运行候选上线后需按新遥测继续采样。样本少于100条时同时报告样本数和分位数，不扩模型并发来掩盖等待。
- 冻结全量测试首次回归：`sh infra/test/run.sh` 退出1，962项中1项失败、0错误、0跳过；失败为`test_new_live_edit_supersedes_old_inflight_media_version`。新编辑的实时媒体上传仍同步等待在Telegram事件回调中，测试里上传释放信号要等回调返回才会发送，最终超时并把新版本标成failed。原失败证据已保留在对应`.runtime/test-evidence/`目录，需修复后重跑。
- 修复后隔离回归：实时pending版本按群组和版本键调度为最多32个后台任务、全局媒体并发2；周期补偿和实时任务共用版本去重集合；配置切换和断开会取消未完成任务，超限时pending留给持久重试。`test_telegram_collector test_telegram_media_scheduling`在Docker中48项通过、退出0，覆盖回调不等待上传、同版本重试去重和新编辑优先；故障仍可恢复且旧消息/媒体对象不丢失。
- 新作者/回复并发优化首轮测试：Docker定向90项中1项失败，`test_album_has_all_images_caption_reply_and_one_version`的夹具消息没有`get_reply_message`方法；旧实现会将此类异常记为reply unavailable，新任务创建路径没有保留该兼容行为。已修复并在原命令重跑，90项全部通过、退出0。
- 定向冻结验证：最终`python -m unittest test_review_worker test_telegram_collector test_telegram_media_scheduling test_telegram_reconciliation_claims.ReconciliationClaimsTests.test_migration_preserves_existing_event_and_reapplication_is_noop test_runtime_settings test_hermes_images -v`在Docker中90项通过，退出0。包括最终结论和数据库时钟计时、复用Hermes HTTP客户端以及相册兼容性修复。
- 图片字节遥测调整后的第一次原命令重跑：90项中1项错误，测试替身省略`media_objects`字段导致KeyError；正式消息字段存在，已将无媒体情况按0字节处理，原命令重新冻结验证。
- 第二阶段低延迟优化已写，完整冻结套件待验证：Telegram作者与回复读取并发；worker复用有明确连接数上限的异步HTTP连接池访问配置、Hermes和会话清理；图片读取最多2路并发；每个worker进程配置最多8个连接。结论耗时并入同一条系统结果写入，使用数据库时钟记录事件创建至结论，减少一次独立追踪写入。实际模型约9至17秒且共享专家限流为1，3秒硬目标仍未达到，不能以本地线程掩盖上游推理或跳过完整工具证据。
- 全量冻结Docker套件初次结果：`sh infra/test/run.sh`退出0，964项通过、0失败、0错误、0跳过，用时392.132秒；证据`.runtime/test-evidence/1045991/backend-tests.json`，冻结源文件哈希已记录，基于HEAD `87a9b81`及工作树内容，`committed_source_verified=false`。隔离恢复演练45张表和1个媒体对象，前后业务盘点差异为空。
- 阶段提交：`879638569c33a76e0960c6c596c1e9073d8b2c9b`（`perf: reduce expert review pipeline latency`）。该提交的第二次完整Docker冻结套件`sh infra/test/run.sh`退出0，964项通过、0失败、0错误、0跳过，用时394.675秒；证据`.runtime/test-evidence/1063937/backend-tests.json`，`committed_source_verified=true`。恢复演练45张表和1个媒体对象，盘点差异为空。待完成分支同步与候选运行验证。
- 分支同步：工作树 HEAD `cc8d63f24c59f35b8d3c17c4df2e4d019b0fd836`，远端`origin/fix/TASK-78-hermes-telegram-sync`核对同SHA，推送退出0。
- 候选镜像：API与Telegram collector分别按CPU配额0.5核、内存512MiB构建`2.0.0-dev.hermes.20261028.4`，OCI revision均为`cc8d63f`；Compose在显式加载既有`.runtime/remote/local.env`后配置校验退出0。未触碰正在运行的`.3`。
- 020迁移预检：只读确认线上数据库仍为019；先生成项目内`.runtime/perf-backup-20261028/pre-020.dump`，30,790,210字节、权限0600，SHA-256校验与PostgreSQL 16 `pg_restore --list`均通过。第一次隔离恢复在256MiB tmpfs不足、添加`v2_current_quotes`主键时退出1；未写生产数据库，待更换隔离恢复存储后按原备份重跑。数据库升级与候选运行均尚未执行。
- 备份恢复缺口修复：表过滤归档遗漏触发器函数`v2_review_schedule_keys()`，隔离恢复失败；改为完整`public` schema备份`.runtime/perf-backup-20261028/pre-020-public.dump`，6,743,504,430字节、0600、SHA-256校验退出0，`pg_restore --list`398项并确认函数、触发器与任务表数据均在。首次完整恢复因目标库预置`public` schema失败，现已在专用Docker卷的新数据库内重新执行恢复；此时隔离目标约25GB、恢复仍在进行，未触碰生产数据库。
- 当前只读运行快照：旧候选`.3`仍运行，数据库019；审核任务1条running、2条queued/retry，四个来源租约有效。由于有活动审核，暂不滚动重启worker；需等任务终结并重新读取租约/队列，避免取消活动模型请求。
- 全量恢复容量复核：项目数据库`model_predictions`约142.2GB、`model_inputs`约35.1GB，本机总可用磁盘仅约26GiB。为避免恢复任务耗尽磁盘，停止并移除本任务创建的隔离全库恢复容器和临时卷；完整6.74GB public schema归档及SHA-256/TOC清单保留。全库生产备份的本机完整恢复仍未验证；迁移演练改为只覆盖新平台`v2_*`数据和所需触发函数，不对远端历史数据做删除或缩减。
- v2隔离迁移库：从完整备份恢复全部`public` schema结构后，确认调度函数和旧触发器已恢复；第一条受限v2数据恢复命令因`pg_restore`表名模式过滤没有命中数据，schema副本目前表为空、未执行迁移。已定位为恢复选择参数问题，将原命令改成对仅含v2表的数据归档直接恢复全部数据后重试。
- v2隔离迁移最终复核：仅包含v2行数据的归档已正确恢复，迁移前`v2_analysis_jobs=2293`、`v2_expert_messages=3703`、`v2_telegram_events=3703`；候选迁移器首次应用020退出0，第二次重放`applied_count=0`。最终隔离库19条迁移记录、最新020，通知触发器1个、`v2_review_schedule_keys()`函数1个，业务行数保持上述计数。
- 候选运行准备：任务备份`.runtime/perf-backup-20261028/pre-020-public.dump`重新执行`sha256sum -c`退出0；由Docker运行的Compose候选配置校验退出0，API/worker镜像revision均为`cc8d63f24c59f35b8d3c17c4df2e4d019b0fd836`。只读预检查确认线上`running=0,ready=0`后，以Compose停止空闲review-worker，退出0。
- 本机API/审核worker实际更新：Compose候选迁移服务应用020退出0（`applied_count=1`），随后`docker compose ... up -d api review-worker`退出0，API与worker均运行候选`.4`。API容器内`/healthz`和`/readyz`均HTTP200；只读数据库再次确认19条迁移记录、最新020、通知触发器1个，`pg_stat_activity`确认worker LISTEN连接正在监听`v2_analysis_job_ready`。迁移未修改旧表或模型/比赛数据。
- 更新后观察：审核队列没有running/queued/retry任务；最近2小时共有76条模型结果，提供方耗时p50 10942.9ms、p95 32131.2ms，旧版本结果缺少端到端结论字段，且候选上线后尚无新实时消息样本，因此不能验证候选的新端到端分布。3秒目标仍未达成；单次真实模型响应本身已超过3秒，且完整检索、盘口绑定与系统校验仍需保留。
- 真实视觉模型路由只读探针：Hermes网关`/models`返回200且列出可用路由；绕过Hermes适配器直调提供方`/chat/completions`时，当前和候选模型均返回502 `upstream_error`，因此后续通过现有Hermes API测试。使用Hermes路径、无生产工具/业务数据的合成纯红像素图：`gpt-5.4-mini` 3866.7ms且识别正确；`gpt-5.5-openai-compact`单次2515.8ms，三次复测2317.2/3207.7/2259.4ms，但有一条未识别红色，另一次32x32纯红图在2933.5ms误报橙色；`gpt-5.6-luna`识别正确但耗时3220.9ms。样本只衡量单轮简易视觉，不代表专家任务；所有探针未写入业务库、未改Hermes/模型配置或切换生产路由。更快候选尚未通过语义质量和完整工具审核验收，不切换现有深度模型。
- 部署边界：实际运行的远端Telegram collector仍为此前同源镜像，`.4` collector只构建在本机；只读GHCR检查返回该候选manifest unknown。为遵守远端镜像同源摘要清单，未用旧executor/tunnel镜像冒充新源码或绕过校验，远端采集端优化尚未部署。需要发布同一源码commit的远端collector/executor/tunnel不可变候选镜像并完成既有部署门禁后，才可验证新的Telegram接收与媒体路径。API/worker部署不等于整个采集到模型链路已完成。
- 本轮实际命令/退出码：Docker隔离迁移器重复运行0；`docker exec ... psql`结构/数据计数探针0；`sha256sum -c .runtime/perf-backup-20261028/pre-020-public.sha256` 0；候选Compose `config --quiet` 0；空闲worker `stop` 0；Compose迁移0；Compose更新API/worker 0；Docker内健康探针与只读遥测探针0。下一步为准备经批准的同源远端开发镜像集、完成collector滚动更新，并等待真实实时消息积累新版本端到端样本；当前业务服务可用，不能承诺3秒硬上限。
- 隔离恢复资源清理：`docker rm -f ai-football-v2-v2restore`、`docker volume rm ai-football-v2-v2restore-data`、`docker network rm ai-football-v2-v2restore-net`均退出0；完整生产备份保留，本机API、worker、远端link未停止。

## E109 专家推荐始终为零的业务链排查（2026-10-29）

- 当前目标/阶段：恢复真实专家消息到可核验推荐的业务链，先定位零推荐的具体原因；研究中。责任范围采集会话续期、Hermes赛事匹配、审核结果与页面展示。前置依赖为有效乐鱼授权、实时赛事盘口、TG信任消息和真实模型；不生成测试推荐填充生产。
- 初始命令/证据：`git status --short`退出0、工作树干净，分支`fix/TASK-78-hermes-telegram-sync`、HEAD `2be61b8`。Docker内只读探针退出0，实际`v2_expert_signals=0`，488条实时审核未解决；不是前端计数丢失。最新TG入库04:08 UTC，赛事最近成功更新却停在10月28日12:49 UTC。远端控制HTTP200、TG已授权，乐鱼状态`unavailable/renewal_state_conflict/409`、连续失败842次；WebSocket 403。
- 现有证据与缺口：明确推荐的图文已被Hermes识别，但真实目标赛事不在已加载候选中；普通聊天被正确保留未解决。首先查持久续期状态为何阻塞及对应原始失败，区分真实上游拒绝、未知登录结果和可安全恢复的本地状态错误；不得绕过锁、盲目重复登录或把旧价格标新。
- 验收/失败处理：以导致故障的状态合同复现并修复；Docker测试覆盖正常、冲突、过期/未知、重复与恢复，保持授权和未知状态保护。真实只读核验采集恢复、赛事盘口更新及有效TG推荐工具闭环；没有匹配或当前报价时如实记录原因。下一步读取续期尝试类别、配置版本及时间，不输出凭据。
- 设计与实现阶段（2026-10-29）：远端最后一次续期为`proxy_login_connect_failure`，旧实现却记录`login_submitted=true/unknown`，此后所有采集恢复均被409永久阻断。Docker内检查实际部署的httpcore连接/代理源码退出0，确认TCP/TLS连接失败与代理CONNECT拒绝发生在应用登录请求发送前；读取/写入/响应超时仍可能已提交。采集适配层以专用异常保留“未发送”证据，仅这些明确失败改为`failed`并走原冷却，不按字符串放宽未知状态，也不直接覆写持久记录。已增加直连/代理、连接池、超时、拒绝及取消反例；代码已写，待冻结验证。恢复旧锁必须依据本次已核验证据走现有管理员审计接口，并在新版本部署后执行。
- 定向验证：Docker限额`.5 CPU/384MiB`、只读源码及无网络运行`python -m unittest tests.test_leyu_session -v`退出0，26项通过（含8组发送前失败和6组可能已发送的失败/取消反例）；连接失败走冷却、响应504/读写失败/取消仍为未知、6031仍为拒绝且不重放。证据`.runtime/expert-zero-20261029/session-tests.log`。下一阶段提交可审查修复并执行完整冻结回归，再发布同源远端镜像集和审计恢复旧状态。
- 冻结验证完成（04:24 UTC）：修复提交`5997467`已推送并用`git ls-remote`核对远端引用。`sh infra/test/run.sh`退出0，966项、401.921秒、零失败/错误/跳过，具名临时PostgreSQL隔离，源码提交与清洁状态已记录，证据`.runtime/test-evidence/2586589`；三项远端角色镜像限额顺序构建退出0。直接推GHCR因现有令牌无packages写权限退出1，未部署无摘要本地镜像；已推不可变开发标签`v2.0.0-dev.20261029.1`，由现有带权限发行流水线`40817107468`完成验证和摘要发布。当前阶段隔离验证通过，真实恢复尚待候选发布。没有重新授权或修改生产数据；下一步核验远端候选与运行容器，再走管理员审计恢复已核验的旧连接失败。
- 候选已发布：流水线`40817107468`的policy、backend、browser、android、publish全部success；源码`5997467`的开发候选及六项摘要已发布，仍非正式生产版本。本机依次拉取collector/executor/tunnel退出0、校验三角色同源与不可变RepoDigest退出0；首次任务计划器缺少`PYTHONPATH`退出1，未产生计划，其后的空计划调用退出2、无远端修改；补齐容器导入路径后原检查退出0。远端现有Compose及桥接源摘要与仓库一致，实盘开关关闭。当前进入远端拉取、停工作器/租约交接及顺序切换阶段；原受保护配置保留，尚未执行重新授权。
- 顺序切换与审计恢复已执行（04:38 UTC）：远端拉取、停止collector/executor、S3带TLS只读检查、Unix桥接配置读取、Compose核验均退出0；旧租约自然到期后4个来源检查点保持单调，活跃来源租约/账户任务/账户续期/订单均为0，本机无采集器或执行器。既有部署门禁输出`eligible=true/blockers=[]`。随后连接组件及远端桥接/工作器均切换到`5997467`三角色摘要，实际容器源码检查`pre_send_fix_loaded=true`、各1实例、实盘关闭，命令退出0。受保护旧镜像配置备份保留，没有回滚令牌或清理原数据。仅对已核验的原始连接失败调用现有管理员重新授权接口，HTTP200且产生审计；生产首次恢复轮询进行中，真实行情与推荐结果仍待核验。
- 行情恢复真实验证（04:45 UTC）：管理员恢复之后的实际登录尝试04:38:21至04:38:45返回`success`，持久状态`ready`、配置版本11；后续已有6轮成功采集、连续失败0、赛事更新至04:44:54。WebSocket有真实更新投递，连接阶段仍随重连变化，不能把一次connected解释为持续可用。切换初期5次轮询失败保留，后续成功不抹去该记录；API日志存在数据库截止和桥接503，当前轮询已恢复，长期延迟仍需观测。`runtime-status.py` Docker只读执行退出0，证据`.runtime/expert-zero-20261029/after-restore-*.jsonl`及收尾快照。
- 真正推荐验收仍未通过：生产`v2_expert_signals`和公开接口仍为0。只读复核最新10条含图片或推荐线索的审核，真实模型能抽取球队、玩法与方向，但目标赛程不存在、已完场、复盘图或其他项目不在当前候选内；最近3小时抽查30条为聊天、推广、空消息等。曾读取盘口的明确推荐发表于10月28日，已过原比赛时段；不会将其绑定未来同队比赛或批量重放历史积压。新增接收也包含旧来源历史补同步，不以接收时间当作新推荐时间。原始报价ctsp即使刚收到仍可能陈旧，采集全轮快照在本次抽样中的15秒有效报价为0；没有扩大TTL、改写源时间或生成占位推荐。
- 四源接收检查（04:47 UTC）：四个来源租约均有效、检查时间持续推进，账户任务/账户续期/订单均0，实盘开关关闭；补同步状态包括`catching_up`及预算延期`reconciliation_deferred`，不宣称四源所有历史均已追平。Docker运行`handoff_probe.py`与`current-opportunities.py`均退出0，证据分别为`handoff-final.json`、`current-opportunities.jsonl`（私有目录，仅文档记录聚合结论）。没有发现可在当前比赛阶段和报价证据下合法重试的明确推荐。
- 部署网页真实点击（04:41 UTC）：Docker Chromium在1440及390宽度实际访问当前HTTP网页，专家列表/详情、管理员登录、TG消息列表、真实模型说明与系统回执均成功；模型文字、系统回执逐字与实际API一致，计数0也与实际存储一致，JS错误和弹层横溢均0。证据`live-clicks.json`及两张截图，命令退出0。首轮脚本误用列表不存在的`analysis_state`字段选取样本而退出1，改为读取前5条详情选择已完成真实审核后原交互重跑成功；未修改产品断言或写合成业务数据。
- 当前阶段结论/下一步：续期误锁修复已提交、候选发布并完成远端行情恢复联调；“真实专家推荐成功产出”仍未验收，依赖新的有效专家意图、精确赛事与当前有效盘口同时具备。966项及发行流水线通过只证明相应测试范围，不能把生产0推荐写成修复完成，也不能承诺3秒上限。记录本轮最终证据、移除任务生成且已不再使用的注册表临时令牌副本，随后提交/推送证据文档并核对远端；原配置、归档和业务数据保留。
- 收尾检查：状态探针一次10秒`ReadTimeout`退出1；原10秒截止、原命令重跑退出0，隧道健康为healthy。状态接口还要等待Telegram授权检查，因此本轮保留这次间歇超时，未扩大截止或声称控制面始终稳定。任务生成的注册表认证副本已删除、私有证据目录权限收紧0700；仅新增/修改两份脱敏文档，差异空白检查退出0、常见凭据格式扫描零命中（rg无匹配退出1）。
- 阶段同步（04:51 UTC）：恢复报告及证据提交`95a561a`已推送，`git ls-remote`核对远端同SHA，命令退出0；工作树干净。最终原截止重跑快照为12轮成功、连续失败0、配置11、WebSocket connected、TG authorized/connected；最新赛事源时04:48:53，推荐表仍0，行情仍为partial且报价freshness stale。当前结论为续期故障真实恢复、推荐生产验收未完成；后续由新的有效消息与匹配盘口完成验收，不把阶段提交当作完整交付。

## E110 零推荐剩余缺陷修复（2026-10-29）

- 当前目标/责任：继续修复真实专家意图到赛事、有效盘口及推荐台账的剩余断点；范围为采集协议、Hermes审核与业务校验。前置为E109已恢复行情及现有TG/模型配置，状态研究中；不把续期恢复视为零推荐问题完成。
- 初始证据：分支保持`fix/TASK-78-hermes-telegram-sync`、HEAD `89f5178`、工作树干净。Docker只读探针退出0：采集成功368轮、连续失败0、TG与行情连接有效，但近一小时81条审核仍未解决、实际推荐0；最新图文存在当日多项推荐，需逐项检查匹配及盘口拒绝。原始证据保存在受保护任务目录，不把原文或秘密写入文档。
- 实现与验收计划：先复核当日推荐的工具轨迹及上游报价语义，定位可复现的程序缺陷，再冻结代码运行对应反例和全套门禁、按同源候选更新相关服务并真实只读联调。不得扩大报价TTL、伪造源时间、把模型自由文字计为已确认推荐或重放历史积压填数。失败保留原检查和退出码，修复后重跑；下一步读取最新六项推荐的完整工具拒绝原因。
- 设计/代码阶段：当日07:18六项推荐有完整唯一绑定；四项提交均因报价陈旧拒绝，其他读取存在采集锁等待和MCP超时。现有只读单场请求1511ms，响应ctsp仍比读取早18秒，证明稳定价格与当前读取不能混为一个时间。已记录`CURRENT_QUOTE_OBSERVATION.md`：保留原ctsp及15秒值，新增精确当前详情复核证据，严格绑定原生选项/价格/状态/比分；只用于推荐记录，订单源时间门槛与账户预检不变。独立异步精确通道、配置版本竞争保护、提交前复核及逐场立即提交已写；尚未验收，不声称推荐已恢复。
- 针对性隔离验证：冻结源码在Docker具名测试数据库运行审核、采集刷新、时间证据及HTTP/WS单调性76项，24.896秒、退出0；补充长轮询与精确读取竞争、配置版本改变、复核不随WS合并继承、入库失败不发确认及MCP传输后，再在无网络限额Docker运行57项，2.726秒、退出0。原ctsp不变与订单fresh_quote仍拒绝陈旧报价有明确反例。证据`current-observation-targeted.log`及`current-observation-contracts.log`。下一阶段冻结全套回归；实际服务尚未更新。
- 冻结验证完成（10:38 UTC）：源码`a76bfdd`已推送并核对分支引用；`sh infra/test/run.sh`退出0，979项、396.625秒、零失败/错误/跳过，证据`.runtime/test-evidence/3268020`。恢复演练45张表、1个媒体对象，业务盘点差异为空。同一提交远端流水线`40858117583`的backend/browser/android均通过。只读核对三场当日明确推荐仍为scheduled、12:15 UTC开赛，盘口存在，线上推荐仍0；仅重试对应原消息，不重放历史积压。辅助只读脚本初次误用表名退出1，按实际结构修正后原查询退出0。当前阶段为隔离验证通过、准备开发候选`v2.0.0-dev.20261029.2`及实际部署，尚未称真实推荐恢复。
- 上线预检发现兼容缺口（10:46 UTC）：只读核验旧模型治理已持久化revision 1基线，旧提示指纹与运行worker一致；直接更新系统提示会触发既有溯源拒绝。保留已批准的固定提示与旧模型配置，新工具语义及逐场提交由既有专用技能和项目上下文提供，新增实际旧指纹的持久基线消费回归。未修改治理记录、未伪造对比审批。该修订使`.2`候选不进入运行，待新冻结验证与`.3`同源候选；当前推荐仍0。
- 兼容修订验证完成（10:54 UTC）：提交`f6cc392`及不可变开发标签`v2.0.0-dev.20261029.3`均已推送并以远端引用核对。Docker模型治理专项11项、12.657秒退出0；完整冻结`sh infra/test/run.sh`980项、396.885秒、零失败/错误/跳过、退出0，源码清洁提交验证通过，证据`.runtime/test-evidence/3302365`。恢复演练45表、1媒体对象，盘点差异为空。未部署的旧`.2`流水线通过GitHub取消接口返回202，避免继续发布已被修订替代的候选；`.3`流水线`40860292903`已通过policy及Android，后端/浏览器和镜像发布继续中。当前真实消息尚未重试、推荐仍0，下一步按摘要核验并部署同源镜像。
- 部署阶段开始（11:02 UTC）：`.3`的policy、backend、browser、android均success，publish进入同源六镜像构建推送。只读核验当前仍0推荐，原消息剩余三场均scheduled、12:15 UTC开赛、各38条active盘口，当前审核活动队列为空。已准备按不可变摘要拉取、四源检查点与租约交接、真实单场复核、原消息审计重试及双端点击验收；不批量重放历史，不进行真实交易。旧模型配置及已批准提示保持原值。
- 同源切换完成（11:10 UTC）：发行流水线`40860292903`全success；五角色镜像摘要与`f6cc392`源码核对通过。首次计划容器非root用户无法读取0700证据目录退出2，改为仅该离线工具容器user0后原计划退出0，未放宽目录权限。远端镜像拉取、停止、只读依赖验证、四来源租约自然到期及检查点单调交接均退出0，门禁eligible=true；API/MCP/连接组件及远端collector/executor已更新同源摘要、实际采集代码检查通过，Hermes已重启加载技能，实盘关闭且订单0。新worker已启动。
- 真实单场复核：启动连接恢复期第一次10秒请求ReadTimeout退出1，原截止与原断言重跑退出0；上海申花目标真实请求写入行情投影，7102ms，38条active盘口全部取得严格当前复核证据，15秒TTL未改、原ctsp保留。下一步仅通过管理员带审计重试07:18原消息，验证推荐实际入库；此刻尚未宣称推荐恢复。
- 真实重试失败与新增根因：原消息已通过管理员审计接口201创建任务；首轮600秒截止退出为timeout，实际推荐仍0。盘口多次38条can_record=true，但Hermes默认50K字符阈值把完整60条盘口转为缓存文件引用，受限工具不能读取；日志保留skill_view越目录拒绝及空signals校验失败。实际缓存正文48–60K字符，传输封装后超过默认阈值。当前修复Hermes配置：MCP单结果使用框架既有100K上限，200K单轮总预算及工具白名单不变，不开放文件/终端能力。下一步用固定Hermes版本的真实渲染与预算函数核验完整盘口留在模型输入，再继续原任务真实审核。
- 传输修订行为验证：Docker内固定Hermes v0.22.5的真实MCP渲染、预算及maybe_persist_tool_result函数逐份读取19个实际完整盘口缓存，最大封装56476字符；100K有限阈值下均保持原文逐字段相等、不转存不可读引用，100K通用单结果与200K单轮预算不变，退出0。证据`e110/actual-hermes-budget.jsonl`。该配置由框架每次预算解析读取，当前原消息第二次自动尝试仍保留同一任务审计及完整资格检查；推荐入库待验收。代码冻结后运行回归并提交当前配置阶段。
- 传输阶段冻结回归完成（11:33 UTC）：`b550978`已推送并核对远端引用；Docker完整980项、406.005秒、零失败/错误/跳过、退出0，证据`.runtime/test-evidence/3379699`。真实运行已确认新盘口完整进入模型，模型随后对明确盘口发起非空提交，但11:27数据库返回DeadlockDetectedError，实际未入账；后续刷新失败及陈旧价格拒绝均保留，最终任务未解决，不能勾选恢复验收。
- 新活动工作包：推荐写入与行情投影的锁顺序修复，责任为API审核/行情事务及隔离并发测试。当前证据为record_expert_signals的联表`FOR SHARE OF q,f`与采集先锁赛事后写盘口存在相反顺序；目标为统一按上游赛事编号锁定赛事，再读取并锁定报价，保持消息租约、原始时间、价格/比分和工具证据完整校验。先用真实PostgreSQL并发反例复现，再修复、冻结验证、部署并通过原消息审计重试；锁超时或入账失败继续标为未完成，不扩大TTL、不清理数据。
- 锁顺序反例与修复验证（11:35 UTC）：具名隔离PostgreSQL中，采集先锁赛事、审核并发提交、采集随后更新报价，旧实现稳定触发LockNotAvailableError，1项测试退出1，证据`e110/record-lock-before.log`。修复后先按source_id共享锁定全部选中且已绑定赛事，再锁报价；同一反例验证采集顺利更新、旧价格拒绝、重新读取后正确价格入账。审核/工具证据/行情原子性/时间确认57项、41.364秒退出0，证据`e110/record-lock-after.log`。当前阶段为隔离验证通过，准备冻结完整回归；线上仍0，真实恢复验收未完成。
- 锁修订冻结回归完成及部署开始（11:42 UTC）：源码`0f8d314`已推送并核对远端引用，Docker完整981项、400.799秒、零失败/错误/跳过、退出0，证据`.runtime/test-evidence/3400636`；45表/1媒体恢复演练业务差异为空。`sh tests/browser/integration-check.sh`退出0，桌面/手机真实API及身份/账户25组场景通过，证据`.runtime/web-integration-3403673`，交易均为具名隔离合成回执、上游交易调用0。不可变开发标签`.20261029.4`指向同一源码，远端流水线`40866866659`执行中，尚未称正式发布。已构建并核对同一源码的本地开发API镜像，准备仅更新API/审核worker；采集位置、数据库、TG连接、模型和订单许可不变。随后通过审计接口对原消息重试并执行线上双端点击，不以隔离数据验收真实推荐。
- 本地开发修订切换完成（11:43 UTC）：`compose ... --no-deps --no-build api review-worker`退出0，两服务healthy，镜像ID`sha256:a6911869120b0e8b4871e119448a7f84896b694be931d5e619167c861b5b0631`，OCI源码标签为完整`0f8d314`，实际审核模块SHA256 `6d0d1278ff75b331a7856c18555f2327e34194d793719ab2cbd1342f421c57c4`并检查已加载赛事先锁实现。该本地开发镜像待远端同源摘要替换，不冒称正式资产。原消息经管理员CSRF及幂等审计接口201生成新任务`81eb3862-9c8a-4ebc-bcb3-615843b606f0`；仅此当日原消息重试，无历史积压重放、无信号直接写入，真实入账仍待验收。
- 真实入账恢复（11:48:39 UTC）：该原消息由真实Hermes工具提交，系统接受上海申花+0.5、金边皇冠-0.5两条推荐，数据库从0恢复为2；价格快照分别2.25000、1.84000，原上游时间及独立当前确认哈希均保留。只读核验命令`docker exec -i ai-football-v2-api-1 python - < .../probe-recovery.py`退出0，证据`e110/lock-recovery-1.jsonl`，订单仍0。当前审核尚在处理剩余未解决项，不提前终止任务。开发候选`.4`流水线`40866866659`的policy/backend/browser/android/publish已全部success；下一步为线上桌面/手机真实点击及空闲后替换同源不可变API摘要。
- 线上点击首轮失败（11:50 UTC）：Docker真实浏览器脚本退出1，等待页面元素30秒超时，证据`e110/live-clicks-failed-1.json`；保留失败并增加准确步骤、调用栈和截图定位，不降低推荐数量/原消息绑定断言。只读确认原审核已于11:49:11进入succeeded、两条信号仍在、活动队列为空；网页验收恢复为未完成，继续定位。
- 点击定位（11:52 UTC）：第二轮桌面全部通过、2条推荐和原消息审核均可见；手机在脚本连续关闭弹层并跳转同文档hash时停留加载，真实接口4.320秒返回200而脚本30秒超时，截图及步骤保留`live-clicks-failed-2.json`。验证脚本现等待弹层关闭和历史导航结束，再通过不同查询串执行完整文档深链接导航，分别验证完整页面启动与原消息显示；业务代码和2条真实推荐断言不变，待原场景重跑。
- 候选摘要切换开始（11:54 UTC）：`.4`发行资产已下载并校验来源`0f8d314`，API不可变摘要`sha256:010eaa4234394567e944ecdbef7890faaf875103d4472ce88a6ef154eed442e0`已拉取，实际OCI标签核对同源码；只读预检活动审核0。仅更新API/审核worker，不迁移数据库、不重启远端采集；切换后重跑线上双端原场景，暂不结束验收。
- 候选摘要已切换（11:55 UTC）：Compose `up --wait --no-deps --no-build api review-worker`退出0；两服务实际镜像均核对`010eaa…`，API healthy，审核模块SHA256与成功入账的本地构建逐字节一致。持久运行覆盖配置已同步新摘要，避免下次重启回退。真实双端点击在该冻结运行版本上重跑，远端采集与数据未改。
- 切换后点击：桌面全部通过；第三轮手机登录返回429，页面正确显示“尝试过于频繁”，系重复浏览器验证累计触发每账户15分钟5次门槛，保留`live-clicks-failed-3.json`及入口HTTP日志。未修改或清除限流记录，将等待窗口到期后只登录一次、复用会话完成双端审核场景；公开最新推荐另做可见卡片与真实比赛弹层点击验证。四个TG来源租约仍有效、检查点持续更新，实盘关闭、订单0。
- 公开可见推荐验收通过（11:58 UTC）：Docker真实浏览器`visible-signals.mjs`退出0，1440/390两种视口均真实点击“最新推荐”，2张原消息卡片可见，球队/系统赔率逐项与真实API相符；再点击上海申花比赛按钮，真实赛事详情及本场推荐正常展示，无页面异常。证据`e110/visible-signals.json`和两张`visible-signals-*.png`。只读核对管理员限流窗口11:58:27自然结束，现仅一次登录并复用浏览器会话重跑完整双端审核，不修改生产数据或登录门槛。
- 性能实际边界：该六项原消息真实Hermes审核调用318369.85ms，任务创建至最终确认约338秒；检索、重试及上游/数据库往返仍显著超过3秒。零推荐恢复不等于3秒目标完成，不能用异步并发、接口200或两条待结算记录宣称全链路速度或收益达标；后续速度优化必须保留搜索完整性及精确盘口证据。
- E110真实联调及页面验收通过（12:00 UTC）：最终Docker `live-clicks.mjs`退出0，1440/390全部通过；五个比赛状态按钮实际请求与DOM赛事编号一致、aria状态正确，专家详情可打开，管理员实际UI登录成功且手机复用同一授权会话，消息列表、模型原始说明、系统确认结果及原消息2条盘口绑定均与真实API逐字相符，无页面异常。证据`e110/live-clicks.json`、`visible-signals.json`及双端原消息/公开推荐截图；前三轮失败证据保留。API/worker为`.4`同源码不可变摘要，切换后只读推荐2、任务succeeded、活动审核0、订单0，四个TG来源持续持有租约并推进检查点。零推荐工作包状态为真实联调通过；下一步提交并推送验收记录，3秒性能目标及全平台正式发布仍未验收。
- 验收记录已同步：提交`987533f3438a35e449c1fbb2a4383c7596cf6e2c`已推送`fix/TASK-78-hermes-telegram-sync`，`git push`与`git ls-remote`均退出0、远端引用同SHA，工作树检查为空。已完成零推荐恢复及对应真实双端验收；后续独立跟踪3秒性能缺口，不把开发候选称为全平台正式交付。

## E111 专家原文全状态赛事识别（2026-10-29 14:40 UTC）

- 目标/责任：修复已存在的完场、取消赛事被误报找不到；责任范围API赛事检索/唯一绑定/系统审核结果、Hermes有限工具与中文技能、对应隔离和真实浏览器验收。前置依赖为现有赛事事实和审核租约；不补造历史盘口或实时信号。
- 当前证据/缺口：工作树干净、分支`fix/TASK-78-hermes-telegram-sync`、HEAD `933706b`；只读数据库确认原消息前三场实际存在，状态分别cancelled/finished/finished，而搜索SQL固定只读scheduled/live，造成模型候选为0并误报无比赛。唯一匹配与可推荐性混用，系统摘要只统计已接受推荐，未单列已识别但不能推荐的比赛。
- 设计决策：`phase=all`检索四种状态，显式阶段筛选仍生效，完整候选快照/连续分页/双方精确消歧继续强制；唯一绑定返回当前状态和明确可推荐标记，零候选与多候选分别解释。系统回执独立展示由工具绑定证明的已识别赛事及当前不可推荐原因，既有推荐数量、不可变价格和统计不变。模型已批准系统提示及治理指纹不改，新行为由有限工具说明和中文技能约束。
- 实现动作：扩展阶段参数与检索；补充唯一绑定的结构化资格/原因；将绑定证据写入确定性中文摘要；修改Hermes技能与MCP说明避免把完场/取消称为不存在。先做真实PostgreSQL反例，再冻结验证、提交推送、部署API/MCP与技能，在原消息审计重试及双端页面核对结果。
- 验收场景/失败处理：完场/取消可完整检索并唯一绑定；阶段筛选、同名双赛/青年女足、跨任务和不完整分页继续拒绝；完场/取消不能刷新盘口或录入实时推荐，审核中状态改变仍拒绝；系统摘要区别已识别比赛与已接受推荐；真实原消息前三场正确关联且不增加完场信号。任何失败保留记录并修复后重跑，未验证不勾选。
- 当前状态：设计已记录，准备针对性实现与反例。实际命令`git status --short`、`git branch --show-current`、源码检索及Docker只读赛事/审核回执查询均退出0；下一步隔离用例复现过滤缺口。
- 反例及实现（14:50 UTC）：Docker具名隔离数据库的原检索反例退出1，明确缺finished/cancelled，证据`e111/search-before.log`。已扩展全状态快照、显式阶段及唯一绑定资格，绑定时重读当前阶段；零匹配/多匹配分别返回原因，确定性中文回执展示已绑定但未录入的比赛。首次定向检查48项退出1：发现持久搜索表仍限制旧阶段，另有1个测试模块名误写；保留`e111/search-after.log`。补充021有锁事务增量迁移扩展CHECK，不改历史迁移、不更改原文、比赛、报价和信号；准备重跑原检查及增量迁移验证。当前为代码已写，隔离验收未完成。
- 定向复核修正（14:53 UTC）：66项检查发现旧跨日分页用例预期仅两种状态，以及新迁移用例遗漏任务必填状态，退出1；证据`e111/search-after-2.log`。原跨日场景现验证三页包含完场、赛前、滚球，并逐页校验不可变快照、原始球队和完整游标；迁移测试补齐合法任务后验证旧数据逐字段不变、新状态接受、未知状态拒绝与重复迁移无动作。业务校验未放宽，准备再次冻结重跑。
- 定向验证通过（14:53 UTC）：Docker限额`.5 CPU/384MiB`只读挂载源码，执行`python -m unittest test_reviews test_review_summary test_review_tool_evidence test_mcp_transport test_migrations -v`退出0，66项、45.106秒，证据`e111/search-after-3.log`。四状态分页/精确消歧、状态变化、取消完场拒绝读盘、重复/跨任务/失效、工具证据重放、中文回执及020→021增量均通过。进入提交冻结与完整回归；线上尚未更新，不把定向通过视为真实修复完成。
- 完整回归发现旧断言（15:00 UTC）：源码`a2bb89e`已推送并核对远端；989项、406.148秒，仅旧Telegram迁移用例硬编码019/020清单失败，实际正常新增021；退出1，证据`e111/full-tests.log`。保留数据完整性和重入断言并补充021预期，准备定向与完整入口重跑。迁移前完整public结构及v2数据备份（146KiB/42MiB、0600）已恢复至专用Docker临时库，前置/数据/后置恢复均退出0；021演练applied_count=1、重复0，1328条搜索无非法状态、9条信号及0订单保留。API/MCP同源码本地开发镜像已构建核对，尚未切换线上。
- 浏览器隔离通过（15:04 UTC）：修订提交`8920974`已推送并核对，原失败迁移测试补齐Telegram隔离环境后1项、1.643秒退出0（首次缺该环境的跳过保留，不作通过证据）。`sh tests/browser/integration-check.sh`退出0，真实API的桌面/手机及身份/账户25组通过，合成执行器上游交易调用0，证据`.runtime/web-integration-3826087`；完整989项正在以同一冻结源码重跑。开发候选`.20261029.5`指向同提交，远端流水线验证中。API/MCP本地开发镜像OCI标签已核对完整`8920974`。备份SHA256校验退出0、临时恢复容器已删除，备份保留。线上另有活跃审核，待其结束后顺序切换；已有推荐消息按现有不可变保护不允许重试，真实联调改用同场江原/古晋的另一条原始未解决消息`9513cc31…`，经只读核验现有审计重试资格有效，未伪造原文或修改既有信号。
- 切换准备（15:07 UTC）：线上最后运行任务已自然转入retry、lease_until为空，只读核验running=0后停止空闲审核worker；保留该任务原重试时间、attempts及全部数据，不取消或清空积压。现有结构和v2数据备份已验证可恢复；021仅扩展搜索CHECK，恢复旧代码必须构建保留021结构契约的兼容镜像，不能以旧镜像绕过结构检查或回滚原始数据。API/MCP/worker切换及真实联调仍未验收，完整回归重跑仍在执行。
- 冻结回归通过（15:08 UTC）：`sh infra/test/run.sh`重跑退出0，989项、410.916秒、零失败/错误/跳过；证据`.runtime/test-evidence/3824420`，源提交`8920974`、冻结清洁状态及逐文件SHA256已记录，恢复演练45表/1媒体业务差异为空。线上空闲worker已停止，Compose独立迁移退出0、applied_count=1；正在仅切换API与MCP，尚未恢复领取任务或重试原消息。代码冻结后未改业务、测试或模型上下文，仅追加阶段记录。
- 本地开发候选切换（15:09 UTC）：Compose API/MCP `up --wait --no-deps --no-build`退出0、健康；空闲Hermes重启刷新工具说明及只读中文技能，随后Hermes/审核worker恢复运行退出0。API镜像ID`c3fc75…`、MCP`34254d…`，均为完整`8920974`源码标签；运行覆盖配置同步，等待远端同源不可变摘要替换。真实原消息经管理员登录/CSRF/幂等审计接口201生成任务`7085407c-8faa-4ebd-ba0f-66196f5b9deb`，仅这条未接受消息重试；受理不等于模型识别通过。远端采集、原始证据及订单许可未变，真实模型和双端网页仍待验收。
- 真实模型识别通过（15:10 UTC）：该原消息由Hermes完成两次完整检索（江原3候选、古晋城1候选）及唯一消歧，成功绑定`leyu:5602656`江原FC/古晋城、亚足联冠军联赛二、finished；没有错配另两场赛前赛事。工具返回realtime_eligible=false、eligibility_reason=finished，模型未调用读盘/录入；系统回执明确“已唯一识别但未进入实时推荐…已完场”。任务按无新增推荐正常保留unresolved，消息信号0、线上自然累计信号10、订单0；不把unresolved状态误称再次找不到。只读快照重放退出0，证据`e111/real-model-2.jsonl`；提供方33702.744ms，领取到结论43540.027ms，3秒目标仍未达成。容器API三模块及Hermes技能SHA256均与冻结源码一致；远端Platform verification/Android边界已success，发行流水线仍在运行。下一步双端原页面实际点击及发行摘要核验。
- 线上浏览器首轮（15:11 UTC）：退出1，旧脚本要求11:49的两条推荐仍在游客“最新5条”中；线上自然新增至10条，公开接口正确只展示最新5条，旧推荐未丢。保留`output/playwright/e111-20261029/live-clicks-failed-1.json`。脚本现对游客实际可见5条逐项比对球队/赔率，在管理员原消息中继续强断言原两条信号及1.84/2.25不可变赔率，并新增已识别完场消息的真实页面/API逐字核验；业务与权限代码未改，网页验收恢复待重跑。
- 真实双端通过与发行摘要切换（15:15 UTC）：Docker Chromium重跑退出0，1440/390均通过五阶段请求/DOM/aria一致、公开最新5条球队/赔率、专家详情、管理员真实登录、原文/模型/系统回执，以及原消息2条1.84/2.25不可变价格；目标已完场消息唯一绑定1场、接受0条，正文逐字与API相符、无横溢和页面异常。证据`output/playwright/e111-20261029/live-clicks.json`及双端截图。发行流水线`40897468787`全部success，`.20261029.5`资产源码/技能哈希已校验，API摘要`013688…`、MCP`510ce1…`已拉取并核对完整源码`8920974`。活动审核只读为0，准备将这两个开发候选及worker切换同源不可变摘要后再次检查真实页面；不改远端采集及持久数据。
- 发行摘要切换完成（15:16 UTC）：空闲worker停止、Compose API/MCP/worker `up --wait --no-deps --no-build`均退出0，实际三服务分别运行上述API/MCP不可变摘要、源码标签一致；持久覆盖配置已更新，避免下次重启回退。API及MCP就绪、worker运行，未重复迁移、未重审已接受消息；本地构建版本双端结果保留`live-clicks-local-build.json`，当前在发行摘要版本重跑同一实际点击场景。
- 最终发行版双端验收（15:19 UTC）：同一Docker真实浏览器场景退出0；1440/390两种视口全部通过，包括五个赛事阶段、5条公开推荐、原消息2条不可变价格，以及完场原文唯一绑定与0条新增实时信号。证据`output/playwright/e111-20261029/live-clicks.json`、`e111/live-clicks-release.log`及双端截图；无页面异常。实际推荐10、订单0、实盘关闭，API/worker/MCP为发行不可变摘要。E111真实联调通过；3秒性能目标仍未达到，未宣称完整平台交付。

## E112 专家推荐随赛果自动结算（2026-10-29 15:22 UTC）

- 目标/责任：修复专家预测在比赛结束后未正确结算；责任范围采集赛果事实、API版本化结算台账、专家统计及真实页面。依赖为乐鱼可核验完场比分、推荐时盘口/赔率证据；禁止猜比分、填零或通过实盘验证。
- 当前证据/缺口：API已有赛果入库及重建接口和分期间亚洲盘结算函数，但初始检索未发现collector调用赛果接口。需只读核对线上推荐、比赛阶段、比分、赛果版本及待结算原因，再核对真实乐鱼字段和采集循环；不能把finished标志直接当作完整确认比分。
- 设计/动作：先确定断点及源协议证据，再实现有界、可重试、幂等的自动赛果投递/重建路径；保留每次来源、结果版本和旧结算，缺少全场/半场/滚球基线时保持明确待结算原因。验收正常、重复、乱序、修订、缺比分、权限拒绝及恢复，再以真实推荐核验结算及页面统计。
- 当前状态/失败处理：研究中；源码检索退出2是引用不存在的测试文件，现已定位实际`test_result_ledger`/`test_settlement`，未运行或写入业务。任何失败保留证据、修复后重跑，不修改历史原文和价格。下一步Docker只读运行快照和真实协议核对。
- 协议断点与替代证据（15:36 UTC）：线上10条信号，2条已完场但比分NULL、赛果及结算表均0。正式赛果列表复用配置11返回空列表，指定今日日期超时25.3秒；当前赛程2073场已不含这两场，盘口详情也移除。按项目内已核验Android DEX的`OBRemoteDataSource.c0`调用链取得原生比赛详情GET路由及mid/rdm参数，真实返回上海申花0:1（上半0:1）和金边皇冠2:0（上半0:0），足球csid1、mmp999，ms分别4/3；两响应哈希已保存`e112/native-detail-*-2.jsonl`/`native-detail-second.jsonl`。最初通用采集客户端路径白名单拒绝该只读研究路径，修正探针URL构造后成功，未改生产白名单或业务数据。DEX `OBScorePeroidEnum`明确S1=FULL_TIME、S2=FIRST_HALF、S7=OVERTIME，证据`score-code-references.jsonl`；此静态观察不冒充模型或实际下注结果。
- 设计补充：只对已有专家推荐关联足球进行有预算的原生详情核验，独立可恢复后台循环，完整身份/终场/S1/S2/S3一致性校验；未核验取消/作废不生成void。原生响应没有赛果源修订时间，新增明确的观测时间和来源未知标记，禁止把查询时钟写成源时间。不可变结果按内容去重，观测排序拒绝乱序，短期追踪更正；保持来源版本、报价证据和结算重建链。增量迁移先备份恢复演练，正常/修订/重复/失效/错误期间/跨比赛/停机恢复均需验证。

## E113 登录会话及Cookie有效期180天（2026-10-29 15:22 UTC）

- 目标/责任：用户及管理员登录凭据的Cookie与服务端会话统一有效180天；责任范围身份服务、生命周期回归及浏览器Cookie验收。依赖为现有独立身份、CSRF、撤销及敏感操作再次验证；最新用户要求覆盖原管理员1小时/空闲15分钟策略。
- 当前证据/缺口：普通登录数据库14天，管理员1小时且读取/敏感复核另强制空闲15分钟；只改Cookie会保留服务端提前失效。登录、刷新和恢复流程共用创建函数，必须同时覆盖；已过期会话不复活，现存浏览器Cookie由下次登录/合法刷新获得新期限。
- 设计/动作：用统一180天期限生成数据库expires_at和Cookie Max-Age/Expires，移除冲突的管理员短期登录限制，保留敏感写操作5分钟再次验证、身份分离、CSRF、登出及密码变更撤销。验收超过旧14天/1小时仍有效、180天边界拒绝、过期不能刷新、撤销立即失效、刷新不能延长敏感验证。
- 当前状态/失败处理：设计已记录，尚未改代码；下一步定向反例与实现，冻结Docker回归及真实双端Cookie检查。不得以放宽权限或关闭再次验证换取长会话。
- 定向隔离通过（15:28 UTC）：统一登录/注册/刷新/恢复创建函数的180天期限，数据库与Cookie Max-Age=15552000及Expires一致；移除管理员读取与锁内复核的旧1小时/15分钟限制，敏感操作5分钟验证与撤销保持强制。Docker专用临时PostgreSQL运行`python -m unittest test_identity test_identity_lifecycle test_identity_mfa -v`退出0，76项82.214秒；证据`e112/auth-tests.log`。新增179天老会话正常、180天到期拒绝、Cookie与服务端一致、刷新轮换并废弃旧令牌及无法刷新敏感验证等场景，原CSRF/越权/恢复/MFA并发撤销回归通过。准备阶段提交及冻结全套验证，线上尚未切换。
- 冻结全量及浏览器通过（15:36 UTC）：源提交`7af0482`已推送核对；`sh infra/test/run.sh`退出0，990项413.180秒、零失败/错误/跳过，证据`.runtime/test-evidence/3885307`、清洁源码冻结。`sh tests/browser/integration-check.sh`退出0，桌面/手机、注册登录/独立管理及账户25组通过，合成上游交易调用0；证据`.runtime/web-integration-3895056`。限额Docker API候选镜像构建退出0、源码标签同提交；开发候选`.20261029.6`流水线运行中，实际Cookie仍待切换后检查。Git提交/推送及远端引用核验均退出0。
- 实现阶段（15:46 UTC）：已加入只领取专家关联足球的远端核验队列、90秒可恢复租约、双并发/每轮4场/30秒循环及失败退避；确认比分、原生身份、常规全场/半场与下半场求和通过后才写不可变结果。未知源时钟单独保存observed_at，重复轮询重建缺失投影但不改变结果版本及原结算时间；比分修订产生新版本，过期租约与跨比赛写入拒绝。022迁移保留原赛果和源时间，补充有界发现索引。代码已写、开始Docker隔离验证，尚未迁移或部署。
- 定向隔离验收（15:48 UTC）：Docker专用临时PostgreSQL的赛果、结算、协议、生命周期及迁移首轮100项27.424秒通过；远端Unix桥新增精确claim/observations/finish白名单并测试角色隔离、拒绝rebuild及任意查询，桥/迁移27项10.411秒通过。补充赛程快照不能清空已确认比分的保护，其8项6.566秒通过。命令均`docker run ... python -m unittest ... -v`，退出0，日志`e112/settlement-tests.log`、`bridge-migration-tests.log`、`projection-tests.log`。准备阶段提交并冻结全套；生产尚无赛果写入。
- 提交与冻结（15:50 UTC）：源提交`51e20ee8e256b94bf8947c4e1be5a86c52f90b03`已推送并以远端引用核对；开发候选`.20261029.7`构建触发。全套在清洁源码副本中运行，未修改被测副本。备份第一条pg_dump因环境URI未作为显式dbname解析而尝试本地socket，退出1；修正为显式受保护连接参数重跑，未更改TLS。候选构建首条使用buildx不支持的`--cpus`退出125，改为既有限额legacy builder的cpu-period/quota与512MiB内存；应用代码未改。
- 备份与演练修正（15:54 UTC）：第二次备份明确遇到应用最小权限角色不能LOCK旧表decisions，未扩大权限；改用既有迁移服务受保护DSN成功备份146KiB完整结构及约43MiB v2数据、SHA256校验通过。首次隔离恢复因新库默认public schema已存在退出1，未迁移；改为另一个具名空测试库，显式删除其空public schema后重跑原恢复。正式数据、角色及TLS未变。镜像限额构建最终退出0，发行资产查询暂404表示流水线未创建开发Release，保留失败记录等待真实完成。
- 隔离恢复第二次失败（15:54 UTC）：结构完整恢复，数据恢复触发旧调度触发器，在pg_restore空search_path中引用未限定表名失败，退出1；原错误与COPY上下文保留。改为第三个具名空隔离库，数据恢复使用pg_restore标准`--disable-triggers`，恢复后自动重新启用并验证业务触发器；不更改生产函数或禁用线上检查。
- 恢复资源失败与隔离（15:55 UTC）：第三次在256MiB tmpfs的定向单测PG中恢复，WAL写满触发PANIC后容器退出；错误确认是No space，不是业务迁移错误。全套使用另一个独立PG未受影响。恢复改用任务专属持久临时卷及新容器，CPU0.5/内存384MiB、shared_buffers32MiB、WAL预算128MiB，保留备份并重跑原恢复及022。失败容器不再承担任何测试或业务。
- 冻结全套与迁移恢复通过（15:57 UTC）：`sh infra/test/run.sh`退出0，1008项422.542秒、无失败/错误/跳过，清洁源51e20ee及哈希证据`.runtime/test-evidence/3932240`；恢复业务46表/1媒体差异为空。专属空库完成结构及v2数据恢复，022首次applied_count=1、二次0，10条推荐、0订单、0赛果与0结算保持，触发器及原推荐价格另核对；日志`restore-check-retry4.log`。新恢复容器初次createdb抢在就绪前失败，确认pg_isready后原命令重跑成功。线上只读部署探针一处旧列名失败，改为按真实state统计后重跑；没有生产写入。下一步顺序迁移和候选上线。
- 线上迁移（15:59 UTC）：确认冻结1008项及恢复验收通过后，Compose独立022迁移退出0、applied_count=1，未更新旧表或推荐价格。审核还有1条活动任务；022与旧worker运行协议兼容，保留其进程自然完成，仅切换API到同源本机构建开发候选，之后空闲时更换worker。发行流水线后端/浏览器/Android门禁均success，publish正在实际APK构建，尚无发行资产，不以404宣称发布完成。
- 真实180天Cookie通过（16:00 UTC）：API本机构建源码51e20ee运行健康；Docker Chromium通过真实管理员页面登录，Cookie Max-Age对应剩余15551997秒，session HttpOnly=true、CSRF=false、SameSite=Strict，登录后管理员接口200；退出0，证据`output/playwright/e112-20261029/cookie-180.json`。普通用户及刷新/到期/撤销已由76项定向与1008项全套覆盖。E113真实登录联调通过，发行摘要切换后仍需双端复核。
- 线上结算与页面真实点击通过（16:25-16:30 UTC）：发现022结构虽已入账，`v2_runtime_app`未获新建`v2_result_checks`表权限，远端队列无法领取。运行Docker只读权限计划退出0：46张v2表、45张业务表、无权限边界阻塞；随后按`infra/db/README.md`执行受控`refresh`退出0，migration role不变，v2_schema_migrations保持只读、旧表权限为0。运行角色表权限校验SELECT/INSERT/UPDATE均true。远端采集器核验两场成功：金边皇冠2:0，主让-0.5 @1.84，赢+0.84；上海申花0:1，主让+0.5 @2.25，负-1.00；原生观测时间另存且source_updated_at=NULL，结果版本各1、订单仍0。第一次只读配置探针错误地请求`/internal/config`而被桥接白名单403，确认是探针拼接路径错误，改用线上结果/数据库验证；该403不是采集服务故障。真实Chromium桌面/手机点击退出0：阶段全部可切换、两条已结算推荐可见、cookie 180天、0页面错误，证据`e112/live-clicks.json`及1440/390截图。

## E114 全系统效率与尾延迟调研及优化（2026-10-29 17:00 UTC）

- 目标/范围：基于真实运行追踪和实际浏览器，对Telegram/媒体、乐鱼实时数据、专家审核队列、Hermes视觉模型/MCP、数据库、结算和网页交互逐段定位效率瓶颈；实施安全且有测量依据的优化。业务正确性、赔率新鲜度、分页穷尽、上游配额和用户账户隔离均不可为缩短时间而放宽。
- 研究证据：同一只读查询窗口最近24小时5067条trace，469次实时lane领取；其中424次领取时原文年龄不超过300秒，329条结论样本。该样本消息持久化至结论P50 31.131秒/P95 115.930秒；API至Hermes网关P50 6.653秒/P95 18.384秒；队列P50 12.958秒/P95 93.590秒；图片准备P95 0.1毫秒。成功实时MCP搜索419次P50/P95 3.536/7.235秒，消歧109次2.589/4.719秒，盘口122次13.858/17.326秒，录入32次16.542/24.248秒。Docker只读查询退出0；脱敏汇总`.runtime/expert-zero-20261029/e114/performance-sample.json`，方法、语义和限制记录在`docs/platform-v2/PERFORMANCE_RESEARCH.md`。当前Hermes网关单段P95已经高于3秒，完整端到端3秒承诺不成立。
- 设计与实施：源码确认Hermes v0.22.5虽会按服务器开关并发调度，但一个MCP会话的RPC仍统一持有`_rpc_lock`；固定框架隔离场景测得同会话数据库区间不重叠。现配置同权限的`football`与`football_parallel`独立会话，工具集名准确配置为`mcp-football_parallel`，最多两项独立搜索/消歧/盘口/跨比赛录入使用不同连接；更多项分批，单个游标及同场多项仍顺序/合并。API任务锁改共享锁，保护完成/撤销边界；每任务事务级advisory lock序列化审计序号写入，避免并发证据冲突。禁止并发刷新过期盘口绕过乐鱼节流。工具广告从6项增加到10项，模型输入token成本待真实运行测量。
- 隔离测试：初次17项在新增测试接线处失败（未挂入`read_market`辅助方法），原Docker重跑17项17.212秒全通过。Hermes固定框架验收中保留了定位性失败：场景漏入脚本参数；假模型在第一页提前消歧而被API按预期409拒绝；单MCP连接即使并行派发仍由客户端锁串行；第二工具集名连字符/下划线不一致。最终`bash tests/integration/run_hermes_acceptance.sh`退出0，11项provider契约测试通过；normal、adversarial、length、parallel、framework restart和budget全部通过。parallel用两个独立MCP会话进行两场消歧，插入的两个500毫秒数据库区间确实重叠；260候选完整翻页，系统只接受目标场一次，17项工具证据均通过严格快照重放；订单/真实模型调用均为0。证据目录`.runtime/af-hermes-framework-4068407`，源码冻结SHA清单与镜像摘要在该目录。固定镜像YAML探针因Hermes虚拟环境无PyYAML退出1，正式框架脚本实际加载配置，不把探针依赖记为产品故障。
- 全量冻结回归（17:07 UTC）：`sh infra/test/run.sh`退出0，1014项、0失败/错误/跳过，用时430.367秒；`backend-tests.json`标记`committed_source_verified=true`，测试证据`.runtime/test-evidence/4085695`，隔离恢复46表及1媒体对象、前后盘点差异为空。提交`18e4f43b5a81ffea9e969c2c39967b46d95951dc`已推送，远端分支引用逐字一致。Android与平台验证两个GitHub workflow均success，workflow run 40917258009与40917258010；本地性能改动的完整测试已通过。
- 排队复测：Docker只读查询确认当前queued/retry/running为0。最近24小时424个新鲜领取中，首轮403次队列等待P50/P95 12.44/85.10秒、重试21次36.01/126.20秒，领取时同专家活动租约均为0；本worker 16:03:44 UTC启动后的最近40分钟仅4个新鲜样本P50/P95 10.44/26.29秒、最大28.70秒。样本不足，不提高模型并发，不把24小时尾部直接归因于当前调度器。
- 失败/风险记录：早期只读脚本假设`v2_result_checks`有`state`/`checked_at`字段，按实际022结构改用`failures`/`last_reason`/`last_checked_at`；未做数据写入。一次管理状态探针5秒读超时，日志未能定位，不据此判定采集健康。固定Hermes镜像的PyYAML探针退出1（环境无该依赖），随后改用固定框架完整加载配置验收。一次队列诊断命令的临时Python语句语法错误退出1，未连接数据库；修正后Docker只读查询退出0。
- 候选部署及运行核验（17:13-17:20 UTC）：候选API镜像`ai-football-api:2.0.0-dev-e114-39ccb3e`，镜像ID `sha256:7863308cc1a5ff9169806f6f109c2ef5a1f1088a4f41346cc3932babc35c1871`，OCI revision=`39ccb3e7363db188347b59fb610da5ab6ad826e2`；API、review-worker已在原项目Compose中更新，`/healthz`与`/readyz`均200。Hermes v0.22.5重启后healthy，挂载本提交配置。Compose验证首次因collector处于未选profile而被探针误判退出1；修正为确认collector/executor未选择、Telegram为remote-link后退出0。未重启collector/executor、未运行迁移、未写入业务记录。队列只读复核各状态为failed 48、succeeded 3、unresolved 4779，queued/retry/running均为0；订单仍为0。Hermes容器内按当前配置建立并关闭两个真实MCP连接，退出0：`football`和`football_parallel`均connected、各有4个白名单工具，8项工具均标记parallel-safe。
- 部署后浏览器验收（17:18 UTC）：`sh tests/browser/integration-check.sh`退出0，基于本提交冻结副本和临时PostgreSQL完成第一轮17组真实API浏览器场景、第二轮8组账户与管理点击场景；均无浏览器JS错误，合成执行器交易调用0。证据`.runtime/web-integration-4119797/integration-results.json`及`integration-account-results.json`。隔离测试未启动Telegram或对象存储，因此Telegram管理员枚举与受保护媒体端点返回503；这是隔离依赖缺失，不能作其真实上游联调通过证据。此前E112的Docker Chromium真实运行页1440/390点击仍是既有线上交互证据；本次仅改API/worker和Hermes配置，没有改Web源代码。
- 启动诊断边界：Hermes健康且固定框架验收实际读取项目只读审核技能；启动前config-migrate日志仍提示动态注册的`expert-skills`未在静态配置校验注册表中，并因配置采用只读bind mount而不能写入迁移结果。MCP别名连接、框架工具白名单和技能读取均另有通过证据，该日志不能据此解释为MCP未加载；配置迁移提示仍待独立处理。镜像运行时缺少`rg`导致一次只读环境名探针退出127；另一次Python临时查询语句语法错误退出1，均未连接数据库或改变服务状态。改用Docker内Python只读查询成功，队列复核退出0。
- 收尾状态与剩余证据：性能研究与可安全实施的优化已完成并部署到本机开发候选；全量Docker测试1014项、Hermes并发框架验收、部署后浏览器回归均通过，源代码/进度提交均已推送。暂不调高worker并发或突破Telegram/媒体、乐鱼盘口刷新节流。当前真实审核队列为空，切换后可用实时样本不足100条，故还不能量化线上并行收益、模型新增输入token成本或重新估算P95；下一阶段需在至少100条自然到达的实时尝试上按同口径复测。Hermes网关当前P95为18.38秒，完整端到端3秒目标仍不可承诺；要继续压低需有更快且实测视觉能力的模型/提供方或明确分阶段状态语义，不能靠跳过工具核验实现。

## E115 已结算专家推荐的当前比赛状态显示（2026-10-30）

- 目标/责任：修正已结算决策卡仍显示推荐发布阶段而非比赛当前状态；范围为公开专家决策列表、比赛详情、消息推荐分页/详情 API 和共用推荐卡片。保留 `s.phase` 作为推荐时阶段，不改结算账本、投注阶段或赛果事实。
- 根因证据：旧 `signalCard` 将 `v2_expert_signals.phase` 映射成“滚球/未开赛”状态标签；该值是推荐发生时阶段，不随赛果变化。公开决策及消息推荐接口也只返回该阶段，未提供关联 `v2_fixtures.phase`。Docker只读生产查询显示10条带最新确认赛果的决策都已有 `fixture.phase='finished'`，因此本次呈现异常不是当前数据库的结算状态遗漏。早期探针将 `v2_fixture_results.fixture_id` 与 `v2_fixtures` 用 `USING(fixture_id)` 连接而退出1；按真实主键 `f.id=r.fixture_id` 修正后查询成功。另一个探针的SQL字符串错误退出1，未产生写入。
- 实现：决策列表、比赛详情推荐、消息推荐分页及决策详情均增加独立 `fixture_phase`/`match_state` 字段。卡片分别展示“比赛：完场”和“推荐：滚球/未开赛”，结算状态与单位收益仍使用 `outcome`/`unit_net`。
- 定向行为验收：新增PostgreSQL/API测试先提交确认赛果并真实结算一条赛前推荐，再检查公开列表、比赛详情和消息分页都同时返回 `fixture_phase=finished`、`phase=scheduled`、`outcome=win`。首轮测试误将推荐阶段设为滚球；系统按既有未知滚球计分规则正确保持pending，断言错误，修正为有效赛前推荐后原测试退出0。
- 冻结验证：`sh tests/browser/check.sh` 退出0，31/31浏览器行为检查通过、0浏览器错误；新增真实浏览器点击断言同时检查“比赛：完场 / 推荐：滚球 / 全赢”。`sh tests/browser/integration-check.sh` 退出0，临时PostgreSQL及真实API浏览器回归17+8组全部通过，模拟交易调用0，证据`.runtime/web-integration-788026`。`sh infra/test/run.sh` 退出0，1015项、0失败/错误/跳过，运行417.549秒（机器记录419.656秒，含加载），恢复46表与1媒体对象前后差异为空，证据`.runtime/test-evidence/795710`。`git diff --check`退出0。
- 候选构建与部署（2026-10-30）：提交 `07e11a70f638871c49058e3d8d0801171961c610` 已推送并核对远端引用。API镜像 `ai-football-api:2.0.0-dev-e115-07e11a7` ID `sha256:82971eb71728fb7c1625b00f7b97fd26a4243d839a74b5cec73d2649b40ed1a9`，Web镜像 `ai-football-web:2.0.0-dev-e115-07e11a7` ID `sha256:b38eb6910350ec2b6999cfd1c6f2091a87276c2528e94e95945afc6c9d27ded1`；两镜像 OCI revision 均为 `07e11a7`。按已验证Compose层仅更新api/review-worker/web，未执行迁移、未重启采集器/执行器、未写入业务记录；API `/healthz`、`/readyz`均200。
- 真实运行浏览器验收：Docker Chromium连现有运行服务 `http://web`，桌面1440及手机390分别打开专家推荐并点击“最新推荐”页；每种视口都从真实公开API取5条已结算决策，逐条核对DOM比赛状态、推荐原始阶段及结算结果。两种视口全通过：全部API当前阶段`finished`对应“比赛：完场”，推荐阶段`scheduled`仍显示“推荐：未开赛”，已结算胜/半输/负标签与API一致；页面JS错误0。证据 `.runtime/expert-zero-20261029/e115/live-state-clicks.json`、`settled-signals-1440.png`、`settled-signals-390.png`。部署前审核活动任务0；现有确认赛果与比赛阶段不一致记录0。
- 冻结测试边界：`sh infra/test/run.sh` 在源码冻结后、提交前退出0，1015项、0失败/错误/跳过，恢复46表与1个媒体对象差异为空，证据`.runtime/test-evidence/795710`。测试报告记 `committed_source_verified=false`，因为当时尚未创建提交（父提交为`3c6ff3c`）；没有在测试运行期间改代码，之后提交内容与被测工作树一致。`sh tests/browser/check.sh` 31/31、`sh tests/browser/integration-check.sh` 17+8场景及定向PostgreSQL/API状态回归全部在提交前通过；真实运行浏览器在提交镜像部署后另行通过。
- 完成状态：展示修复已在本机Docker开发候选运行并完成现有已结算数据的桌面/手机只读验收；未重算任何结果。完整远端生产发布、第三方审核及主线合入不属于本次候选部署证据。

## E116 网盘软件样本收集与 REA 调研（2026-10-31）

- 目标/责任：在`reports/feijipan-share-20261031/`保存用户指定分享中的全部文件、目录清单、校验和与REA静态分析报告；不运行下载的软件，不连接其服务。分享访问口令不写入报告。
- 前置与工作树：当前分支`fix/TASK-78-hermes-telegram-sync`，开始时工作树干净；保留现有`reports/`内容。浏览器下载和样本解析须在受限Docker中进行。
- 清点证据：Docker内固定项目浏览器镜像运行Playwright，只读访问分享页并递归展开目录，命令前缀`docker run --rm --network bridge --cpus 1 --memory 1g --pids-limit 192 -v /root/ai_football:/work -w /work --entrypoint node ai-football-browser:v2.0.0-dev -e ...`，退出码0。共6个根目录、13个子目录节点、34个文件；页面返回总量1,511,639 KB（约1.44 GiB）；14个已访问目录页均满足返回条数等于总数。清单阶段未下载文件，磁盘可用59 GiB。
- 设计决策：保存原始下载并保留分享目录层级；对所有文件计算SHA-256及字节数；只解包软件归档，不执行安装/启动程序；REA结论标明Evidence ID、静态证据范围、推断和未知项。若文件缺失、密码保护、损坏或不受支持，逐件记录并保留失败原始状态，不以占位文件或文件名猜测成功。
- 实现动作：先下载全部34项并验证数量/大小/哈希；再识别归档格式、隔离解包并递归清点；按实际包类型调用REA分析，最后生成报告与证据索引。
- 验收场景/失败处理：原目录树完整；每一项有下载状态、源路径、实际大小及SHA-256；归档完整性通过；软件包由REA返回可引用证据；无法覆盖的材料明确列出。下载失败时对原文件重试一次并保留两次错误，不扩大到其他来源。
- 下载证据（2026-10-31）：34项均已落盘，实际总字节1,547,917,217；逐项复算字节数与SHA-256退出0，`evidence/download-manifest.json`的`failures`为空。首次下载脚本在全部文件保存后因收尾汇总代码`TypeError`退出1，未掩盖该失败，随后完成逐项校验；没有把失败命令改写为成功。原始文件保留在`downloads/`，分享目录层级完整。
- 静态拆包（2026-10-31）：20个RAR与4个ZIP在具名Docker工具容器中完成静态解包/清点，未执行样本；`evidence/archive-inventory.jsonl`记录归档条目。REA对RAR容器本身不支持，因此使用`unar`展开后分析托管程序集和JavaScript；一个ZIP由REA完成中心目录检查，5个文件条目、4条关系、0个完整性矛盾。
- REA分析证据（2026-10-31）：11个主要业务DLL完成托管元数据分析；PT21深入成员分析得到283类型、2,889方法、17,911调用边、8,934字段访问和544用户字符串，选择性账户/设置/许可分析及PT21参数扩展目录分析均有Evidence ID。USDT小工具为原生PE，未做原生反汇编；所有样本均未运行、未登录、未抓包、未下注。
- 报告产物（2026-10-31）：`reports/feijipan-share-20261031/REPORT.md`、`evidence/rea-evidence-ledger.json`、`evidence/package-static-summary.json`、`evidence/inventory.json`已写入，报告区分静态事实、教程声称、推断和未知项，并记录解包与REA边界。报告/证据一致性检查退出0：34/34哈希、14个目录节点、21条REA证据和11个程序集摘要互相可引用；敏感信息扫描未发现分享口令或配置值。临时静态解包容器已清理，工作包“调研报告复核”完成，剩余依赖为无。

## E117 网盘样本重点专项分析（2026-10-31）

- 用户重点：判断病毒/木马风险、用REA解释体彩/体育跟单原理与盈利方式、总结对本项目的启发；分析仍限于静态证据，未运行样本、未登录、未抓包、未下注。
- REA新增证据：DB跟单选择性分析覆盖主/跟账号、token/API域名、比赛/玩法/赔率/金额、金额规划器、派发队列、轮询、余额刷新和反向投注构造；证据`ev_87988d358fc48c01659f6f2f92302476aa517dab595780e9cc388c0e0d60a438`、`ev_c99602bbecacc478e19d1dc66ca2359e5a58bdb0c03d239e3501e3f8090a547a`、`ev_27155560681cdcb020c27d699a4539e36e05c938b7d34f609f51a1ca7422144e`。SB跟单新增赔率流、订单状态、预结算和下单方法证据`ev_cc97de222581537f8cff0d820c8b1d401569d97050612bfcc0774dc7e3afca98`、`ev_d55a243e18426d906f1314cd8cc9bf3fb8c723f6e5104cebde11f7493aa6e54f`。
- 风险结论：主跟单DLL均为IL-only现代.NET并含Windows反调试P/Invoke声明；这是供应链/反分析信号，不能单独证明木马。USDT原生PE无法由当前REA原生提供方深入分析，仅完成PE资源清点，存在`MINIDUMP_EMBEDDED_AUXILIARY_PROVIDER`资源；不作无恶意结论。
- 报告更新：`REPORT.md`新增“是否存在病毒/木马”“体育跟单原理与盈利方式”“对本项目启发”重点章节；`evidence/copytrade-rea-findings.json`固化专项证据。报告引用一致性和`git diff --check`退出0，敏感值未输出。

## E118 Telegram专家发言漏采与倍率匹配约束（2026-10-31 11:00 UTC）

- 目标/责任：修复多群 Telegram 历史追赶/媒体/编辑回补阻塞实时消息，以及广播频道帖子缺少独立 sender ID 时被错误过滤；明确模型按赛事和盘口意图灵活匹配，专家原文倍率绝不与系统赔率比较。责任目录为`services/collector/`、`services/api/`（现有工具合约行为验证）及`integrations/hermes/`。
- 当前证据：生产只读观测显示6个来源租约仍active且Telegram授权连接正常；4个来源有消息事件，2个`trusted_count=0`来源没有信任作者映射，因此它们按当前规则不会形成专家消息。collector真实日志出现历史追赶/重conciliation与实时回调竞争、实时锁等待约8–10秒、HTTPStatusError/history deadline。API桥连接上游最大请求窗口10秒。群/作者映射相同不自动判错：依照Telethon官方`Message.sender_id`说明，它是可缺失字段，广播频道帖子可能没有独立发送者；因此保留显式配置的负数频道作者映射，不做自动改写或信任升级。
- 设计决策：每群锁只保护检查点租约核验和CAS推进，Telegram RPC、事件入库及媒体提交在锁外执行；历史/作者回补仍先持久消息再推进各自游标。仅当`post=true`、sender ID为空且该群ID本身已在信任列表中时，将广播频道自身作为作者；普通群组及匿名管理员缺失作者ID仍拒绝。比赛/盘口匹配允许简称、别名、翻译及口语表达按语义匹配；赛事必须唯一，玩法/期间/方向/盘口线须落到已读取的确切系统quote；专家赔率只留作原文，不作比较、门槛或系统计价。
- 实现与验收动作：收窄采集锁持有范围，覆盖频道身份回退边界及慢历史提交下实时回调场景；在真实隔离PostgreSQL/API测试中核验原文倍率3.80与系统赔率1.95不一致仍接受对应盘口，signal/evidence只保存系统赔率。更新Hermes独立技能/中文上下文，保留服务端盘口新鲜度、停盘及唯一赛事约束。
- 当前状态：代码已写，隔离验证未开始。后续依次执行Docker定向测试、冻结全套测试、限额构建/候选部署；然后真实只读观测各群新事件、锁等待及回执。未在本记录时点直接写入生产作者信任映射；两个`trusted_count=0`来源需通过受保护Telegram搜索确认其当前专家身份后再配置，不可批量信任管理员或普通成员。
- 命令/退出码/失败处理：只读源码/测试检索`rg`均退出0。一次容器内环境探针依赖不存在的`rg`，退出127；一次临时Python语句语法错误退出1，均未读取或输出凭据、未写业务。Telethon语义已用官方文档核验：[Objects Reference](https://docs.telethon.dev/en/stable/quick-references/objects-reference.html)。
- 信任配置只读核对（11:04 UTC）：在API Docker容器通过现有数据库连接执行只读聚合，退出0。六个群均enabled；`-1001358434454`及`-1001890322660`各有1条enabled且enabled expert的显式频道自身作者映射；`-1001971712856`有3条有效作者映射；`-1002413688036`有2条有效映射、另3条来源映射disabled；`-1002427292301`无信任作者映射但目录快照ready且列出11位管理员；`-1002907383508`无信任映射且管理员目录不可用。不能把管理员自动变成可信专家；后两个来源即使能读群也不会保存未配置作者的消息。第一个定向Docker测试调用从`tests.test_*`导入时有2个因未加入`/workspace/tests`路径无法加载，退出1；同时1个旧测试仍硬编码历史/实时消息入库顺序失败，已按新无锁语义改为集合验收。修正导入路径及顺序断言后，Docker命令`docker run --rm --cpus=.35 --memory=384m --network none ... python -m unittest tests.test_telegram_collector tests.test_telegram_media_scheduling tests.test_telegram_directory -v`退出0，81项3.373秒；覆盖频道fallback/普通群拒收、历史慢写与实时提交、媒体、目录和多群并发。此组未连接业务数据库。
- 全套首轮结果（11:15 UTC）：`sh infra/test/run.sh`业务测试本身1006项全部`OK`、耗时416.954秒，但证据收尾因`infra/test/frozen_suite.py`使用`ensure_ascii=False`写入包含代理字符的测试报告而退出1；该退出码未被掩盖，问题已定位为报告编码而非业务失败。修复为JSON ASCII转义后必须重跑原命令，只有重跑退出0才记录隔离验证通过。
- 全套隔离验证通过（11:24 UTC）：修复证据编码后重跑`sh infra/test/run.sh`退出0，1006项、0失败、0错误、0跳过，用时412.903秒；证据目录`.runtime/test-evidence/436801`。恢复演练前后46张表、推荐/订单/媒体盘点差异为空；新增频道身份、锁竞争、媒体回补、目录并发及原文倍率3.80/系统赔率1.95仍按系统价入账的回归均包含在套件内。业务源码测试在冻结副本执行，未连接真实Telegram、乐鱼、Hermes或资金账户。
- 下一步：运行完整Docker隔离套件（需临时PostgreSQL）以覆盖系统倍率记账行为及所有API回归；修复失败后重跑原检查。之后记录冻结源码提交、推送及新采集版本真实运行观测证据。

- 候选构建与远端切换（11:28–11:34 UTC）：提交`d2b5ce2`已推送并核对远端引用。Docker限额构建API、Telegram collector、Hermes MCP均退出0，三镜像OCI revision均为`d2b5ce2`，collector内容摘要为`sha256:1e9c758ade3aaf26e11cdb61f2e4037d4aba040434e485a01f3b8c003c6d79c7`。GHCR推送退出1，原因是registry返回`unauthenticated`；未把失败写成发布成功。随后使用现有受限SSH链路将collector镜像导入远端，远端本地内容ID与候选摘要一致，备份远端`.runtime/remote-workers.env`后仅替换`REMOTE_COLLECTOR_IMAGE`，compose配置退出0。
- 真实远端切换（11:31 UTC）：远端collector按停机45秒排空后重新启动，旧实例确认`exited`，新实例运行且镜像为候选摘要、revision=`d2b5ce2`；executor未重启，仍为原摘要、运行中且`EXECUTOR_LIVE_ORDERS_ENABLED=false`。依赖的api-bridge由compose按既有配置重建并运行；未执行迁移、未修改信任作者映射、未写入订单。
- 切换后90秒监控（11:36–11:38 UTC）：Docker内只读状态连续3次返回HTTP 200、Telegram`connected=true`/`authorized=true`；多个已信任来源每个3分钟窗口持续产生1–2条事件和专家消息，检查点从`3290→3291`、`4778→4779`、`180034→180040`等继续前进，未配置可信作者的来源仍为0条消息，符合权限设计。实时日志锁等待由旧版本约8–10秒降至0–3.2秒，未再出现10秒级等待；历史校对/媒体重试仍有`reconciliation_deferred`、`TimeoutError`、`HTTPStatusError`，均保留待处理状态并继续重试，未阻塞文本事件入库。采集窗口内不执行实盘或订单写入。
- 当前状态：源码修复、Docker全套1006项测试、远端collector切换及真实只读监控均通过；GHCR不可用导致三类候选镜像尚未形成远端registry发布资产，API/MCP仍运行既有候选版本。后续依赖为恢复registry认证后补推同一摘要并按发布清单核验，或继续保留当前SSH导入的collector运行；不能将本次开发候选称为全平台正式发布。

## E119 近期比赛命中问题诊断（2026-10-31 12:52 UTC）

- 目标/责任：查清用户看到的比赛未命中是否发生在赛事搜索、唯一消歧，还是后续盘口记录资格；责任范围为`services/api/`、`services/collector/`、`integrations/hermes/`及审核轨迹。当前状态为研究结论已记录，尚未改代码/配置。
- 当前真实样本：只读审核轨迹显示样本`556a07be…`的6项都产生唯一比赛绑定，另两个样本分别为8/8和3/3绑定；被用户感知为未命中的6项样本实际都已匹配到相应场次。没有推荐记录的主因发生在读盘之后：部分原文玩法不在当前乐鱼市场覆盖范围，其他盘口刷新返回`refresh_busy`、`refresh_timeout`或`refresh_unavailable`，报价`can_record=false`、过期或未确认。系统没有拿专家原文倍率与乐鱼赔率做对比，亦没有为这批审核产生订单。
- 确认的匹配短板：生产只读检查发现12,911条赛事的主客队别名均为空；采集适配器在`services/collector/leyu_protocol.py`把`home_aliases`/`away_aliases`固定输出为空。`services/api/app/routes/reviews.py`的搜索按队名子串或别名精确值建立候选，解析则按候选中的标准主客队名/别名精确相等并要求唯一。系统提示虽说语义匹配，却未明确要求字面查询零结果时按中英文/音译/简称生成替代查询，也未明确最终用候选返回的标准队名调用解析。因此昵称、翻译差异和拼写变化仍可能漏召回；不应通过放宽唯一性或猜 fixture_id 修补。
- 运行版本核验：API、review-worker、MCP 均为`d2b5ce2`候选且健康，之前处理样本的 API/review-worker 和 MCP 分别为`65e62133`、`8920974`，样本处理时未加载 E118 当前提示/回执。当前容器内 worker 提示 SHA-256 为`daa966669d2fd225687b2b041ea1fe8d80d5bd7a039b3a9cb4f76c81a2f42d24`；Hermes容器内技能文件与仓库技能文件 SHA-256 相同（`219ceb658a5eb8638a53d0a946041db86487371371cd6c79234e910290ad8d57`）。提示摘要受模型配置治理校验；修改系统提示而不形成已核验的配置制品会导致 worker 拒绝消费当前配置，故本轮没有改提示、技能或线上配置。
- 下一步设计决策：如进入实现，新增有限且可审计的替代搜索词策略（中英文名、音译、官方全名、常见简称/后缀规范化），每个`search_key`完整分页；从候选结果取标准主客队名、联赛与时间窗口调用严格唯一解析。审核结果应分别呈现“比赛已绑定”“盘口不支持/报价未确认”，避免把未记录推荐误报为未匹配比赛。别名数据只有在获得可追溯来源后才能写入；不放宽报价新鲜度、停盘、唯一赛事及精确盘口校验。
- 命令/退出码：`git status --short --branch && git log -5 --oneline --decorate`退出0；源码定位`rg -n 'async def search_matches|async def resolve_match|team_alias' services/api/app`退出0；容器内 worker 提示摘要与 Hermes/仓库技能摘要核验退出0；Docker容器清单核验退出0。测试未运行（本轮无代码改动）；数据库仅引用本任务之前已完成的只读轨迹/别名聚合，不进行写入。下一步为按用户后续任务授权补充召回策略、针对性回归，并通过模型配置治理证据后再部署。

## E120 专家审核30分钟闭环监控（2026-10-31 13:00 UTC）

- 目标/责任：连续观察真实专家发言审核；每30分钟检查新增消息、任务终态、系统生成的比赛绑定、模型工具轨迹、已接受推荐及未解决原因。若出现错配、漏审、终态失败或原因误报，定位后立即修复、Docker测试并部署；部署后观察计时归零重启。
- 验收：窗口须至少有1条真实、启用且受信任作者的新消息，采集消息与审核任务可对应；所有应审核的实时消息均完成审核，且每项比赛绑定与原文球队/联赛/时间及工具候选一致。过期/未确认报价、非实时阶段和不支持玩法可不记录推荐，但必须保留正确原因，不能伪称赛事未命中。窗口没有新专家发言不算正常通过，需继续观察下一个窗口。
- 安全边界：只读检查线上消息及审核证据；不重放历史审核、不写入生产推荐/订单、不放宽赔率时效或唯一比赛校验。修复候选先隔离测试，只有检查当前运行任务为空且准备好可回滚的同源镜像后才部署。
- 起始状态：2026-10-31 13:03:32 UTC，API、review-worker、Hermes MCP、Hermes容器均运行；`/healthz`与`/readyz`均为200。窗口起始数据库统计全部为0（按窗口创建时间），当前待观察窗口至13:33:32 UTC。
- 首窗发现问题（13:03:32–13:09:38 UTC，未完成）：采样器停止前的快照显示窗口内2条实时发布时间消息、3个审核任务；后续只读详情共定位3条可信作者的当前消息，任务状态failed/retry，均没有`hermes_request`或赛事搜索轨迹，失败安全分类为`RuntimeError/internal`。另有历史补扫条目及无媒体可处理内容，未混作新实时审核。首窗因采样器未包括远端Telegram状态且发现审核系统性错误而中止，不算验收窗口。
- 已确认根因：worker当前提示摘要`daa966…`与受控配置存储的批准提示摘要`222388…`不一致；后者与已批准提交`f6cc392`中的提示逐字计算一致。`resolve_review_settings`在生成`hermes_request`前抛`review configuration artifact provenance does not match worker`，因此近期消息根本未进入Hermes或赛事检索。该版本不一致由`d2b5ce2`改动系统提示但没有对应已核验配置制品造成。
- 修复动作：源码系统提示已恢复为`f6cc392`批准内容；不修改数据库治理配置。当前部署的Hermes专家技能保留语义别名与翻译匹配、专家倍率不得比较、完整分页和严格唯一赛事约束。下一步在Docker冻结套件验证受控基线可消费，再构建并部署同源API/review-worker候选；利用管理审核重试入口处理仅在提示配置不匹配期间失败且仍为最新的实时消息，不重放历史积压。部署后必须以真实回执和新自然消息重新起算30分钟窗。
- 首窗命令及退出码：`docker exec -i ai-football-v2-api-1 python -u - < .runtime/e120-watch.py`于手动中止时结束，最后输出elapsed=360秒（API health/ready均200）；`.runtime/e120-diagnose.py`首次JSONB解码不完整退出1，修正后只读诊断退出0；`.runtime/e120-config-check.py`受控配置核对退出0，明确prompt_matches=false；从`f6cc392`提取的批准提示摘要与远端配置摘要均为`222388e0…`。一条执行在本机的远端容器检查退出1，原因是远端容器并不存在于本机Docker daemon；改走受限SSH检查退出0，确认远端collector、bridge、executor运行。未显示消息正文、昵称或凭据。
- 修复代码及隔离验证：`services/api/app/hermes_client.py`恢复`f6cc392`已批准的系统提示文本，Docker内重算SHA-256=`222388e0dde4957ff096435cdf283f676e1c01e8003c9f536599f76f30a84173`；生产受控配置摘要一致。`sh infra/test/run.sh`退出0，1006项、0失败/错误/跳过，412.138秒，证据目录`.runtime/test-evidence/691034`；冻结期间未改测试源码。
- 部署与消费（13:27 UTC）：提交`59e19fd`已推送；Docker限额API镜像`ai-football-api:2.0.0-dev-e120-59e19fd`构建退出0，Image ID=`sha256:b84ade286fbac8866c744d1c12ad991a6e8111aaa53df58acbcaa19dd3490916`，容器内提示摘要核验与批准值一致。`sh .runtime/e117-20261031/compose.sh config --quiet`退出0；部署前只读门禁queued/retry/running全为0。`sh .runtime/e117-20261031/compose.sh up -d --wait --wait-timeout 120 --no-deps --no-build api review-worker`退出0；API/review-worker均运行于revision=`59e19fd`，API healthy。未执行数据库迁移、未重启Hermes/MCP/远端collector。通过已部署worker的`resolve_review_settings`正规消费路径，配置读取及ACK均成功，使用SHA-256=`222388e0…`；该ACK作为配置消费审计写入既有模型治理台账。
- 修复后实时消息恢复（13:34 UTC）：识别7条当前时间窗口、受信且启用、media ready、零已接受信号、无Hermes请求、失败分类为RuntimeError/internal的失败任务；复用配置中的管理员账号，经Docker API内正式管理员登录与`/api/v1/admin/reviews/{id}/retry`接口提交7项审核重试，HTTP201均入queued，ID仅在本地证据显示SHA-256短码。容器任务脚本未输出账号、密码、Cookie、原文或昵称。13:34:56 UTC只读门禁为queued=5、running=1、retry=0，故队列尚未全部完成，30分钟观察窗暂不开始；此前成功消费与远端状态请求退出0。Telegram远端status HTTP200且connected/authorized=true、last_success_at=13:34:48 UTC，但`degraded=true`，来源统计为1 catching_up、2 reconciliation_deferred、3 unavailable；该采集限制仍待持续检查。

## E121 比赛名称变体召回与唯一消歧（2026-10-31 13:45 UTC）

- 当前目标/责任：修复用户反馈的模型仍不能稳定命中比赛；责任目录为`services/api/`与`integrations/hermes/`，不改比赛阶段、唯一性、盘口新鲜度或专家倍率处理。前置依赖是E120已恢复模型配置消费；当前处于代码已写、定向与冻结全套隔离验证通过，候选待构建/部署。
- 新证据：Docker内只读快照截至13:43:27 UTC，共有6次真实模型请求、9次唯一比赛绑定、3次完整但零候选搜索；6条任务未产出已接受推荐，绑定成功的盘口读取有`refresh_busy`/`refresh_unavailable`/`upstream_timeout`，无`record_expert_signals`。至少一条任务完成8次搜索和5次唯一绑定，说明其失败不属于赛事命中；另有真实审核回执的identified_count=0，单靠该计数无法断言消息包含推荐。当前12条queued、1条running说明E120窗口未通过，Telegram状态仍degraded（来源有补同步延期/超时）。
- 根因与边界：`services/collector/leyu_protocol.py`的赛事主客队alias恒为空；API搜索用字面子串/别名精确值召回，解析再用大小写折叠后的完全相等匹配。Hermes技能未强制在候选返回后采用系统标准队名，因此缩写、重音/标点差异、常见球队词形及模型转译的搜索词可能无法闭合。0次搜索的样本还需要结合消息意图判断，当前聚合证据不能证明其是模型漏判。
- 设计决策：对搜索增加少量可审计的常见足球简称扩展；解析统一Unicode/重音/标点并安全处理俱乐部前后缀，允许信息充分的球队名前缀；必须主客两队同时匹配、应用联赛/时间条件后恰有一个候选才绑定。常见泛词不单独作前缀证据。Hermes必须完整遍历候选并使用返回的标准主客队名称消歧；模型给出的专家赔率不参与任何匹配。
- 实现动作/验收场景：新增`team_matching.py`纯匹配辅助模块；更新`search_matches`简称/Unicode变体候选召回与`resolve_match`双方队名比较；补充重音/标点、`Man Utd`/标准名、清晰前缀、泛词拒绝、同名歧义及U21/女足防串场契约测试；更新Hermes独立技能指令，不改受控系统提示摘要。通过后运行冻结全套测试；构建限额候选、核对配置摘要及队列部署门禁后再让API/review-worker生效。真实验收仍需在修复后窗口验证新的可信专家发言匹配和系统回执，重置30分钟计时。
- 当前剩余依赖：Telegram来源健康仍需本观察窗口持续核验；新部署后的30分钟观察器即将启动，完成前不宣称专家功能完全正常。盘口源超时属于独立问题，禁止为产生推荐而放宽赔率时效。未改线上比赛数据、未产生订单。
- 已执行命令/退出码：`git status --short --branch && git log -5 --oneline --decorate`退出0；Docker内汇总、任务轨迹、来源状态、Telegram状态及源码检索均退出0。临时网络`ai-football-v2-e121-target`和具名PG`ai-football-v2-e121-pg`中运行`test_reviews.py`共36项、32.208秒、退出0；临时PG/网络清理退出0。冻结全套`sh infra/test/run.sh`退出0，1010项、0失败/错误/跳过、415.750秒；机器证据`.runtime/test-evidence/761184/backend-tests.json`报告418.1秒，源快照父提交`e137656`且`committed_source_verified=false`（测试时实现仍在工作树，之后提交的业务源码与测试快照相同）。`git diff --check`退出0，秘密格式扫描零命中。代码提交`6a6fde5`及进度提交`168eab4`、`f7354ec`、`a750e95`均已推送。限额Docker构建API Image ID=`sha256:7f064815465d1b8d2e2655c2603dacece915a3fb4614d4aeac683f0701aadcaa`、MCP Image ID=`sha256:b243b05da0e91f5040c634584c1741de778370246c2748d52f38889483662db0`，均带revision=`6a6fde5`，构建退出0。Compose配置退出0；容器内批准提示摘要仍为`222388e0…`，简称变体/前缀函数和MCP新工具描述检查退出0。Hermes容器挂载技能SHA-256=`51b9f7…`与仓库一致。14:07门禁退出2（queued=1/running=1）；14:08门禁退出0；停止worker后14:10再次门禁退出0（queued/running/retry全0）。按顺序部署API、MCP、review-worker均退出0，revision=`6a6fde5`且运行中；API `/healthz` 与 `/readyz`均200，部署后提示SHA-256仍匹配`222388e0…`。无数据库迁移、无采集器或Hermes gateway重启。下一步启动1800秒只读观察器并记录数据库时钟起始时间。
