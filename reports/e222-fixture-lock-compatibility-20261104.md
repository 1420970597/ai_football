# E222 结算比赛锁与审核外键兼容

更新时间：2026-11-04 14:21 UTC。状态：设计已记录，未改线上锁。

目标：结算更新赛果/收益时继续串行保护同场结果，同时允许审核绑定及候选快照引用既有比赛主键。责任目录services/api、tests、reports、docs/platform-v2；依赖E220固定版本及临时Postgres。当前E220完整30分钟冻结，不提前部署。

证据：E219真实resolve binding INSERT锁超时；E220批量后搜索candidate INSERT仍两次LockNotAvailableError，采集接口当时91请求全200。首轮pg等待见transactionid/advisory，后轮未遇；不能据此确定每次锁错都来自结算。代码确认rebuild_settlements和ingest_result取fixture FOR UPDATE，其事务不改变主键id或唯一source_id；该模式会阻塞引用该比赛的外键KEY SHARE。批量仅缩短持锁，不改变锁兼容性。此为可明确复现的冲突机制，实际故障归因继续采样。

决策：仅将上述两处比赛行锁改为FOR NO KEY UPDATE；仍互斥同场结果写入与普通更新，不允许删除或主键改写，不放松signal FOR UPDATE、任务租约、搜索分页、证据或价格校验。无需迁移或数据改写，算法不变。不得把它称完全无锁或已解决所有超时。

实现与验收：先独立临时PG构建真实审核job/8条真实隔离推荐及正式赛果。让rebuild或新版本ingest持比赛锁后，在project阶段通过async barrier暂停，另一连接插入review fixture binding，要求既有800ms锁预算内成功；原FOR UPDATE负向应失败，改后通过，同时并发第二个结果写入仍受互斥保护。保留版本链/收益/时间/幂等和不可变冲突原回归；冻结后定向及完整套件，再等当前窗口结束idle部署。失败恢复原候选、不放宽锁预算；线上样本与隔离机制验证分别报告。

14:26隔离证据：2个新barrier用例在原实现均实际LockNotAvailableError，4.592秒/2failure/exit1，分别重算与新赛果；第二写入锁断言仍通过，确证不是去掉全部锁。独立候选2f90c0e仅两处锁模式和注释、测试，根分支cherry-pick e58f031；冻结后39项结果台账/结算37.667秒exit0，原800ms引用通过、第二写入100ms仍被阻挡、旧版本/时间/不可变冲突/原native方向回归保持。完整Docker套件同冻结2f90c0e进行中，14:44:01当前窗口结束前不生效。

线上批量后采样新证据：第15/22样本看见review_lease_renewal等待事务锁，阻塞方为审核trace验证/其他语句；这不是fixture锁归因证据，不能将本候选称解决全部实际超时。保持active_job共享锁阻止取消/完成越过工具操作，不以加并发或移除任务锁掩盖。事件API当时91请求全200、模型search candidate INSERT两次锁超时分别记录。

14:38隔离验证通过：同冻结2f90c0e973e0d720445699a08fe1419f63094847完整Docker套件1251项/548.539秒，exit0，失败/错误/跳过均0；机器报告总执行550.946秒，报告SHA9215c8584d858aef2f9b4b415694fd34ec62f05d751c5fd13c2faf5de14c66d1，恢复差异{}。命令BUILDX_BUILDER=ai-football-e131 sh infra/test/run.sh，在独立候选目录执行。随后同冻结源限额buildx API构建exit0，镜像ai-football-api:2.0.0-dev-e222-2f90c0e，digest sha256:1ec26bb61cc0180aea99beb5dccc3da2e52daba12a09e3aa94dc0f49537f267d，完整revision标签一致。线上尚未修改；当前E220日志已出现补采503及审核锁超时，不能称该窗口稳定。下一步完整窗口保存后idle仅切API/worker，再正式15场重放核对原台账及时间不变、真实浏览器、下一完整30分钟。未知滚球计分规则继续保留未解决，不猜结算。

14:51真实联调：14:45:10.675845暂停新claims、14:47:20.175538自然idle后，仅API/worker更新指定1ec26b镜像；deploy exit0/ready200/worker running，gate正常释放。MCP/web/存储隧道ID、StartedAt、资源不变，Hermes/采集未改。正式15场rebuild HTTP200，同算法；前后38条原推荐报价哈希、38结果、80结算版本/created_at全部相同，Docker校验exit0。真实Chromium桌面1440/手机390各2条文图审核详情全部通过，exit0：原图加载、模型/系统同attempt、正文与API一致、无JS错误/横溢。14:50:16.383240新1800秒完整冻结窗启动，首样本健康/6有效租约/单owner；监控聚合补充state=connected及source_errors为空，避免前窗布尔healthy遗漏矛盾。15:20:17前不切后续候选。当前只是发布后初步联调，完整稳定性/遗漏/长尾仍待终审。附诊断首次误查不存在claimed_at exit1，改读attempt_claimed trace重跑成功；旧多场第4attempt仍timeout/retry，未把基本健康当模型恢复。
