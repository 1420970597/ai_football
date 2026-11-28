# Telegram无用户ID频道监控核验与足球滚球来源研究

## 工作包（监控核验及公开来源清单完成）

- 目标：核实当前线上系统能否接收无个人用户ID的频道帖子，并形成尽可能广且可核验的公开足球滚球TG来源清单。
- 责任目录：services/collector/、services/api/、tests/（只读核验及针对性验证）；docs/platform-v2/、reports/（研究与进度）。
- 前置依赖：当前API a6debc6b及现有collector镜像、受保护Telegram配置；不加入/发言/更改来源，不执行投注。
- 当前证据：根目录源码存在广播频道post且sender_id缺失时按显式信任chat_id匹配的逻辑及对应测试；需核对实际镜像、入库与审核、现有来源和隔离测试。
- 缺口：代码支持不等于生产已配对或已收到样本；公开频道的存在/滚球内容与可获利不是同一证据。
- 设计决策：区分广播频道、频道身份发言、普通群匿名发言；匿名个人不可被推断。只读核验当前来源和近期事件脱敏计数；互联网优先原频道预览和官网交叉链接，多语搜索去重。清单按明确滚球证据/混合待观察/排除项分级，注明抓取时间、可见性、来源和局限，不将订阅量或自报盈利当可靠战绩。
- 实现动作：核对镜像源码哈希，检查接收→信任映射→去重→审核链路；有缺口时先记录证据，再最小修复；独立研究公开来源并生成Markdown与JSON清单。
- 验收场景：缺sender的post显式频道信任可采；普通群无身份不冒充频道；负数频道身份可配置；编辑/相册/回补边界有说明；每个候选可追溯公开链接、内容匹配和证据级别，无实盘盈利保证。
- 失败处理：公开预览不可访问仅列未核验，不绕过私有访问；生产缺样本不造假，隔离样本与生产事实分别报告。保留现有账户未提交修改。

## 阶段记录

开始阶段：Git与工作树已核对，未改业务配置。首次rg包含不存在的猜测路径退出2，改按实际路径继续只读。下一步当前镜像/真实脱敏计数与专项测试、互联网公开来源采样。

### 2026-11-12 11:35 UTC 监控链路核验完成

结论：现有线上实现支持没有个人用户ID的广播频道帖子，前提是该来源显式信任其自身`-100…`频道身份。普通群的无身份消息不会自动冒充频道发言；频道签名`post_author`不是稳定用户ID，系统不能由此识别背后的个人。Telegram官方契约中`from_id`、`post_author`均为可选字段，`post`区分频道帖子：https://core.telegram.org/constructor/message 。

当前生产源码以`.runtime/refactor-wt/web-ux`的`49ba719d`为核验参照，主检出业务树较旧，不能用于覆盖线上。实际API镜像`sha256:a6debc6b0401b2ffbcc2e8ec6283d52f1e89b5eb51438eb2f93a30b82ef79616`、collector镜像`sha256:710c68254436bad35321b7b5bd2245a017ddeb6dd78b8d583c36c57dfef3afa0`。实际容器五个责任模块与该工作树SHA256逐一一致：

| 模块 | SHA256 |
| --- | --- |
| collector/telegram.py | 27dc9b7743627ef955d3419d34d897d23deee5d465957b80d51f77f8dfd4b1fb |
| collector/telegram_common.py | 3a913adb072d9bacaee69db28cc6a18efe9ee2df413284273840eb27d65b6e36 |
| collector/telegram_backend.py | 8cec8beb90c8dca556618792408379298846be8cad94c5b192c94f1c224ae3c7 |
| API/routes/telegram.py | 23018ef6f4c30be565ad2ba6aae71b1a18ac4305739bd5c4ae801d1581074902 |
| API/routes/admin.py | 473678f89b71bcf27eede6164b58c1b7c19d20ce859d5024b0e9735c4f6519f9 |

接收路径：`_trusted_message_author`仅在`sender_id`为空、`post=True`、当前`chat_id`已显式受信任时回退为频道身份；已有负数posting peer保持原身份。API接受非零64位带符号数字身份，按来源启用映射精确绑定；不把手工频道身份标为已识别个人管理员。入库、去重、媒体待就绪及审核任务沿现有持久链处理，不依赖昵称猜测。编辑产生新版本并使旧审核失效。

只读TLS生产查询时间为2026-11-12 11:19:13 UTC：11个启用来源均有检查点及活动租约；2个信任自身频道身份，近72小时分别20及59条事件版本，共79条；对应审核11项succeeded、37项unresolved、18项failed。追加只读原因核验：18项failed全部为`source_edited`，属于原文编辑使旧任务失效，不是缺用户ID接收失败。unresolved仍是未解决，不算推荐成功。入库已将身份归一，79条不能证明原始消息全是`sender_id=None`，也不是79个去重帖子；无sender分支由隔离样本验证。另3个启用来源没有任何受信任作者，因此本窗口无事件，属于配置缺口，不能仅凭来源启用就声称会采其全部帖子。来源名称、真实chat_id、个人ID及配置秘密均不写公开报告。

边界：新增负数频道身份不会进入仅限正数个人ID的专门作者7天回补任务；主来源检查点、实时及近期对账继续有效。本次不承诺新增映射后已自动补齐全部频道历史。匿名群管理员或以频道身份发言不能还原为真实个人。图片/私有来源是否可读、权限是否存在和审核是否找到比赛属于独立门禁。

冻结专项验证在Docker内、具名临时PG执行：

```sh
cd /root/ai_football/.runtime/refactor-wt/web-ux
TEST_SUBNET=10.93.89.0/28 sh infra/test/run.sh tests.test_telegram_collector tests.test_telegram_sources tests.test_telegram_directory
```

结果：87项、7.664秒（以冻结JSON为准）、失败0、错误0、跳过0、退出码0。冻结证据`.runtime/test-evidence/2240589/backend-tests.json`。运行器原始`committed_source_verified=false`保留：工作树有未跟踪`.omc/`，没有改写原标记；另在Docker内将153份已跟踪API/collector模块及专项测试逐一与冻结清单比较，差异0，工作树已跟踪文件无修改。覆盖缺sender广播、普通群不得回退、负数频道身份不伪造个人、来源作者隔离、编辑、相册、媒体重试、持久检查点及断线对账。实际生产读取使用`docker exec -i ai-football-v2-api-1 python -`执行受保护脚本，事务强制`default_transaction_read_only=on`且查询超时8秒，最终两个查询均退出0；摘要保存于本任务受限`.runtime/telegram-channel-audit-20261112/`。

定位过程失败保留：最初猜错容器模块路径、从工作树使用错误相对路径的哈希预检、SQL排序引用不存在的`created_at`均退出1；依实际目录/列名修正后原检查通过。未关闭校验、未改变数据库/来源/登录/下注开关，未通过真实投注验收，无需发布业务补丁。当前活动转为公开来源去重与分级清单；规划提交`f721defb`待本阶段一并推送。

### 2026-11-12 11:36 UTC 公开来源研究与归档验证完成

产物：`reports/telegram-football-live-20261112/README.md`完整中文清单、`sources.json`机器可读65项、`channel-monitor-audit.json`脱敏线上/测试摘要、`check-public-previews.py`可复查脚本。中文、英语、葡萄牙语、土耳其语检索结合Telegram原频道预览、官方服务互链。目录只用于发现；HTTP200、订阅数、自报胜率和名称包含live均不作为可靠推荐证据。公开页约20条采样无法证明持续活跃/删除编辑完整性，更不能证明盈利；全部`profitability_verified=false`。

65个唯一公开handle逐项匿名GET：A优先观察5、B已见滚球但需复核12、C自动信号/工具7、D入口或持续滚球未证实21、H超过90天历史样本7、X排除8、U当前无法核验5。A/B共17项观察候选仍不是17名收益核验专家；同品牌、转载及同工具分别记录，不能按入口数计算独立性。另附Football Trading Club、Statz两个官方服务/私有群入口，不重复计数。群聊公开正文不可读时仅标入口，未加入或推定群内质量。

有效性复核避免误判：GG101s最新可见2025-05-22，VOC为2023-02-28、StatsNBet为2023-10-11、Live Tips Free为2022-07-01，均不当作当前活跃。GolvarGPT的live关键词实际多为应用宣传和赛前战报；BT4Y明确说明事后赢家帖不代表之前在Telegram发过推荐；VIPservice仅简介/不完整样本不足以确定滚球，均列D。Guru/GoBet/BETSBOT/BetBugler等自动化单列研究附件，不引入已移除算法业务。营销“内幕/蛇货/无损”、FIFA电竞及纯套利软件排除。图片、角球/牌数/球员属性、组合盘、短时段及交易所进出仓不假定能转换为本项目已支持的单次下注契约。

实际执行与验收：公开抓取使用精确API镜像的`python /evidence/fetch-public.py`，54+11个handle分批、另13项人工复核，`httpx`匿名GET，超时15秒/并发3，Docker预算0.5 CPU/256 MiB/128进程；各批退出0。受限临时目录只作现场证据，不提交原文/图片、凭据或私有身份。封存生成命令为：

```sh
docker run --rm --network none --cpus .5 --memory 256m --pids-limit 128 --user 0 \
  -v /root/ai_football/.runtime/telegram-channel-audit-20261112:/evidence:ro \
  -v /root/ai_football/reports/telegram-football-live-20261112:/output \
  -v /root/ai_football/.runtime/refactor-wt/web-ux/.runtime/test-evidence/2240589:/test-evidence:ro \
  --entrypoint python sha256:a6debc6b0401b2ffbcc2e8ec6283d52f1e89b5eb51438eb2f93a30b82ef79616 \
  /evidence/build-catalogue.py
```

该命令退出0；65项唯一性、分类计数、A类近期/H类停更时间约束、HTTPS链接和冻结87项结果断言通过。另Docker验证JSON结构/65项合计/全部盈利未核验/79事件版本/7.664秒原证据，编译复查脚本并只对首项执行一次公开GET烟测，得到`checked=1, errors=0`及`schema_and_audit_validation=passed, single_public_get_smoke=passed`，退出0。完整匿名65项复查命令已写README，避免为了演示重复全量。`git diff --check`限定责任文档退出0，限定产物秘密模式扫描无匹配，其他账户未提交修改保留。

结论：用户要求的监控排查和带证据来源清单已完成。线上无需补丁/重建，本次没有生产写入、加群、联系频道、订阅付费、添加来源或资金测试。仍需后续独立授权及验证的事项为来源/作者和图片权限、连续建议追踪、盘口唯一绑定与独立统计；不能把本清单当作实盘盈利背书或完整平台发布。当前阶段仅提交、推送和远端SHA核对。
