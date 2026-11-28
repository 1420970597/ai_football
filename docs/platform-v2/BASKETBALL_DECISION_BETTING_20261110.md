# 篮球专家决策、投注与结算

更新时间：2026-11-10 14:57 UTC。状态：篮球原生全场执行、无加时结算及可信终局冲突的受限修订通道隔离验证通过；源码已冻结交付父代理。真实投注与发布验收未执行。

目标：在原有本人许可、专家策略和限额下支持乐鱼篮球原生全场独赢、大小、让分的专家推荐及自动投注；补齐可审计篮球结算，供足球/篮球分项准确率使用。责任目录为 services/execution_scope.py、篮球独立测试、证据确认后的现有结算及结果采集链路。赛前代理统一修改执行器 account_protocol、API orders 与 dispatch；统计/UI 由独立代理负责。前置依赖为现有 csid=2 采集、原生盘口身份、账户隔离和无资金协议样本。

当前证据：E336 真实只读篮球全场 38/total、39/handicap；E362 原生 39 的 hv 为主队线，客队展示反号但执行请求保留原生线；E2781 真实 37/winner/full_time、空线、ot=1/2。原生篮球37/38/39已进入采集及工具投影，现有 API eligibility 与 dispatch 却硬限制 football/live，且订单快照 sport_id 硬编码1。原执行器仅允许 live，最新盘口和订单体使用通用 sportId 但缺少篮球玩法/期间的组合校验。

缺口：原 settlement 对全部篮球恒为 pending；原生结果采集仅足球，不能靠页面比分推测含加时规则。历史 E505 仅一页50笔足球回执，篮球投影为空，不能作为篮球结算证据。旧官方普通体育规则仅声明专规优先，不能推导篮球加时口径。继续查原生篮球规则与结果显示链，未确认前保留 pending，不套足球滚球扣基线。

设计决策：复用唯一执行器与原生标识，新增纯函数共享 scope，篮球仅接受经核验的37/winner、38/total、39/handicap/full_time；足球既有市场与期间保持兼容。未知体育、跨玩法、半场/单节不转换成全场。赛前/滚球由当前phase映射决定，用户实盘开关和许可不变。没有真实资金测试、历史补投、生产写或部署。

实现动作：先建立共享 scope，再交由赛前代理整合 API eligibility、队列 before-LIMIT 过滤与 NativeSelection 校验；独立篮球测试核对 sportId=2、正确原生 chpid、方向及原生盘口线。结算须另有官方或真实普通回执规则、权威最终比分期间证据、加时一致性检查和修订版本记录后才允许落地。

验收场景：37主客、38大小、39正负线主客的赛前与滚球请求；原文赔率变化不阻止准确盘口预检；跨体育、错误chpid/kind/period、独赢平局拒绝；账号隔离、限额、停盘、行情变化和未知回执重试限制保持；篮球独立结算覆盖加时、走盘、取消、修订及证据不足。原生结算证据不足即未验收；隔离通过不冒称生产联调。

失败处理：保存原错误码及范围，不修改旧推荐快照或已有结算；协议未知继续未解决，修复后重跑同一场景。原生规则证据不足则明确外部证据缺口并继续研究。

实际命令与阶段证据：

- 宿主 git status --short、rg 与 sed 只读定位，退出0；错误文件路径 ledger.py/settlement_jobs.py/contracts.py不存在的组合检索退出2，改按真实 result_ledger/result_check_queue 查找。
- Docker 固定镜像 sha256:a8fc0cb5dbebed71df5688f33c3963a076151c7d41ca8e7625520b8ab7ffc234，network none、CPU0.25、内存256MiB、只读挂载读取受保护历史REA。首次镜像默认非root读取0600失败退出1；改 --user0:0只读访问后退出0，不放宽文件权限。仅白名单字段输出，无凭据。
- Docker 解码旧官方 sportRules 静态资源的中文字符串，退出0：确认无篮球加时专规，不能把普通条款当专项规则。
- services/execution_scope.py 已写；尚未测试，等待协调文件整合与冻结。

14:20–14:32 UTC 阶段：使用现有 storage-tunnel 和远端 collector 既有授权会话，对已识别的5833477/5833464各一次 GET 原生终场详情，零登录/续期/observations/订单写入。可提交的白名单投影见 evidence/basketball-native-finals-20261110.json。5833477 S1=79:86、S2=34:47、S3=45:39、四节22:20/12:27/23:18/22:21，mmp999/ms3/mft0，完整响应SHA ea8782dcfdc03326e613b816ed3ca3dad9de37f4915f6b422ea489fea4e4d699；5833464 S1=87:108、半场37:68、四节19:37/18:31/24:20/26:20，mft0，SHA29e440270b48a2883d620edc662fb1ed8f9f5924c3390186414ad65df84dd8cf。历史 E505 两个NBA mft7样本同样四节总和=S1，所以 mft7不能被猜作加时标志。

真实 REA：使用现有固定6.7.0 CLI/JADX0.7.1/JDK，Docker network none/CPU0.35/768MiB/单worker，classes7.apk SHA029070e8ffe05c8cf1592a945166234d81cf3c13b7d9ee3062fa3fa411c4f145；search Score实际0（ev_a590edab5dbc27b7dbac69a1c943d611430c00042392b8c9aecf1b9e5ad16eda），OBScorePeroidEnum <clinit>完整smali实际0（ev_77277ab0f6f2420110a2f18315ebfb6c9ebdd3d7766794314eb9bc2474dae97a），S1=FULL_TIME、S2=FIRST_HALF、S7=OVERTIME。静态证据不冒充执行App或全体育加时规则。命令分别为 bash .runtime/e209-live/e330-run-rea.sh e330-basketball-score-types search-android-classes /input/leyu-analysis-classes7.apk Score，以及同wrapper e330-basketball-score-enum inspect-android-method /input/leyu-analysis-classes7.apk com.yyds.sport.repository.ob.OBScorePeroidEnum '<clinit>'。

执行参数复核由赛前代理取得 OBBetViewModel.m3完整REA（ev_a80a2ff89961b2a19f9d5fafcd6ec92e3dbc291ec5a5160fcee5d3836bea1148）：matchType来自getOriginMatchType，sportId来自getEventSportId；这证明项目不应固定football，但不证明篮球结算。共享 scope 及 NativeSelection/API过滤已整合，三Dockerfile复制共享模块由赛前代理统一修改。

实现：共享 basketball_scores 对S1/S2/S3/S19..22逐项必需、重复/非整数/缺失均拒绝，四节/半场/整场一致且无S7才标记none_verified。collector只支持已观测mft0/7篮球终场；claim返回sport、API校验期间证据并核对持久fixture sport，不能跨体育投递。篮球37/38/39独立分支以完整全场分结算，39保留主队原生线方向；不调用足球滚球扣分政策。加时、无四节证据、未知期间仍pending；37的平局无规则仍pending。新算法版本 football-asian-9-basketball-no-overtime 保留旧计算行和修订版本，不改推荐快照或真实账户收益。

测试：独立test_basketball_execution首先冻结副本运行5项，其中6个赛前子场景因旧prematch门槛原生退出1；其余场景通过。测试时NativeSelection已有协作代理scope修改，不能把负向通过冒充旧码拒绝证据。补齐独立test_basketball_settlement的完整期间、无加时、胜负/大小/让分、原生方向、重复回执、比分修订和跨体育禁止写入用例。首次针对性9模块冻结到.runtime/basketball-verified，Docker CPU0.25/384MiB、已结束e3056内部PG网络、每项新建test_result_UUID临时库，无生产连接；运行中已有旧回归断言失败，待完整原始输出定位，当前不记通过。

其它失败记录：历史NBA结果为JSONL，误按单JSON读取产生Extra data退出1，改逐行解析后0；e336旧错误文件为空，误读产生JSONDecodeError退出1，改实际02证据后0；公开网页检索只发现无法确认真实提供方身份的自称乐鱼站及其他提供方规则，不能据此改加时政策。

范围限制：真实原文“第四局大40.5”在当前审计中对应market63，当前库存名称仅63、source_63。缺完整原生chpid/期间与结算契约，继续保持未解决，不改成全场；本包明确只支持37/38/39全场篮球，不声称所有篮球玩法已完成。

14:38 UTC 验证阶段：首次102项/148.216秒原生退出1，七处失败均为新算法名称expert-asian字典序排在原football-asian系列之前，旧重算审计测试按该顺序检查旧行。保持既有版本系列名称football-asian-9-basketball-no-overtime后，冻结02重跑同九模块102项/76.988秒，0失败/错误/跳过、原生退出0，日志SHA f421ba8168ef8e477260c1273b9f3583e98b632a52bebdd4e6533587411b69ad。未改旧台账、重算逻辑或禁用断言。补充一项原生篮球结算后真实API篮球准确率0/单位净额-1、足球分项零样本的联合测试，冻结03跑篮球结算全9项/4.485秒再次0；两轮合计103个独立用例。源码与最终冻结副本十个责任文件逐字节相等，验证清单 evidence/basketball-verification-20261110.json（SHA24c3746b109b785b734b49aea083dcc08369eaf370da4a84b9c1f7928adf06d0），白名单真实赛果投影SHA627a1b23a2f6bd2d92c34e4ccf09573c9f9b3377843ec68048fca42079a4a939。

复现原始验证命令（03仅将挂载副本改为basketball-verified-03、模块改为test_basketball_settlement，资源与隔离库相同）：

```bash
docker run --rm --name ai-football-basketball-targeted-02 --user 0:0 --network ai-football-e3056-full-private --cpus .25 --memory 384m --pids-limit 100 --read-only --tmpfs /tmp:rw,size=48m -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/source:/source/services:/source/services/api:/source/tests -e TEST_DATABASE_ADMIN_URL=postgresql://postgres@ai-football-v2-pgverify/postgres -v /root/ai_football/.runtime/basketball-verified-02:/source:ro --entrypoint python sha256:a8fc0cb5dbebed71df5688f33c3963a076151c7d41ca8e7625520b8ab7ffc234 -m unittest test_basketball_execution test_basketball_settlement test_settlement test_leyu_results test_result_ledger test_result_checks test_result_check_priority test_score_rules test_handicap_lines
```

63补查：14:35:49通过现有授权路径读取已结束5833477的原生赔率，当前响应不含该精确比赛行，研究脚本输出AssertionError且未获得可用playData/定义，不能视为成功协议证明；未继续猜编号或重发。记录为负面证据。第一次末尾文档锚点patch失败未修改任何文件，按完整现有文本重试成功。所有新日志、脚本与冻结副本限本任务目录，不改全局依赖或其他项目。

修订缺口与决定：初包沿用旧结果通道，原生S7/期间冲突被当作采集失败；若此前已有无加时confirmed结果，旧结算会保持为最新，这不满足修订后的数据质量要求。父代理确认扩展：仅同已领取篮球赛事、身份/时间/终局状态均匹配且七个比分期间仍为完整合法整数的响应，可用明确S7或期间求和冲突生成provisional新版本。存在S7还须唯一且比分合法，不能把乱码S7、缺字段、普通超时、权限失败或空回应当作撤销证据。

实现设计：复用现有result版本和settlement事务，不删除原始结果/账本。新增纯函数核对冲突证据，将七个期间及确证S7纳入最多20项证据；API在现有租约、fixture sport和来源校验下仅接受篮球provisional冲突，不放开普通football观测。最新provisional使专家投影pending，旧版本不进入最新有效统计；重复同事实幂等；后续完整无加时确认恢复为下一新版本。collector将已提交的冲突报告partial/provisional，网络失败仍仅finish，不投递假provisional。验收含confirmed→可信加时冲突→pending/体育统计排除→幂等重复→证据恢复，期间冲突校验及普通网络失败不撤销。先新冻结专项验证，失败保留原退出再修复；之后统一完整回归。

受限修订首轮：冻结basketball-provisional-red的新正向冲突测试，原码一项/两个子场景原生退出1，确证S7/期间冲突仍被当错误。实现后冻结basketball-provisional-final，原九模块107项/82.232秒原生退出1，只有恢复此前相同比分时发生v2_fixture_results的fixture/provider/content_hash唯一约束冲突；普通网络失败不撤销、冲突转pending与统计排除等其余106项通过。首次失败日志均保留在.runtime/e209-live。

修复设计与实现：不迁移结构、不更新旧摘要或台账。继续使用fixture的FOR NO KEY UPDATE事务锁，在同一锁内读取最新结果、比较规范事实、核验源时钟并分配version。连续重复只与最新规范事实比较；源时钟未知时排除每次轮询变化的响应摘要/观测时间，已知源时钟时保留证据比较规则。新行content_hash为SHA256({version,facts})，使confirmed v1→provisional v2→原confirmed事实v3可以保留三个不可变版本，随后重复返回最新v3；旧provider_reference及旧源时钟重放仍由单调检查拒绝。未执行任何历史批量重算。

期间矛盾边界：完整合法整数但上半场比分超过S1也是可验证的官方冲突。仅原生ObservationInput在完整validate_provisional_evidence通过后允许这一矛盾，通用ResultInput和football继续拒绝，当前租约/来源/sport校验仍在事务内。HTTP验收覆盖原生provisional正例、通用结果/confirmed/football反例，且不产生任何订单。

14:57 UTC 最终验收：源码冻结到.runtime/basketball-provisional-final-02，原九模块109项/84.400秒，0失败、错误或跳过，Docker原生退出0。涵盖v1确认→可信加时冲突v2/pending/分体育统计排除→重复v2→恢复v3→再重复最新v3，旧行逐字节保持；普通ReadTimeout只结束核验不撤销旧确认；完整比分矛盾仅原生provisional HTTP可接受，通用结果/confirmed/football均422；football旧provider重放409、最新重放幂等，既有足球修订/锁/排序测试保持通过。该轮没有生产连接、部署、结构迁移或资金测试。

最终验证清单替换初包清单，包含11个责任源码/测试与最终冻结副本逐字节相等及SHA、命令范围和限制：evidence/basketball-verification-20261110.json，SHA2221592bba9225cf2166a805fc7b44683d4d6f87d0c2a3a914522b9856ebbd76。最终原始日志.runtime/e209-live/basketball-provisional-final-02.log，SHAda1ced677e4431e569c6b3c941440113b1e8955293f0bdcd945d6df51158b6f0。上述14:38 UTC的旧清单SHA描述初包当时证据，已被本轮清单替代，不代表当前源代码。

最终完整原命令（stdout/stderr原样保存至上述日志，进程原生退出0）：

```bash
docker run --rm --name ai-football-basketball-provisional-final-02 --user 0:0 --network ai-football-e3056-full-private --cpus .25 --memory 384m --pids-limit 100 --read-only --tmpfs /tmp:rw,size=48m -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/source:/source/services:/source/services/api:/source/tests -e TEST_DATABASE_ADMIN_URL=postgresql://postgres@ai-football-v2-pgverify/postgres -v /root/ai_football/.runtime/basketball-provisional-final-02:/source:ro --entrypoint python sha256:a8fc0cb5dbebed71df5688f33c3963a076151c7d41ca8e7625520b8ab7ffc234 -m unittest test_basketball_execution test_basketball_settlement test_settlement test_leyu_results test_result_ledger test_result_checks test_result_check_priority test_score_rules test_handicap_lines
```

下一步：父代理统一完整回归、候选构建和真实页面验收。用户本人实盘开关与既有许可保持，不补历史投注。加时收益及63专项规则仍需继续研究；本包通过只代表上述范围，不把无资金测试当作真实资金验收。
