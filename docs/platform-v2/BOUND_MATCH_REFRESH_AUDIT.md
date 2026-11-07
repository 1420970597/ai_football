# 已绑定比赛的有界盘口刷新

更新时间：2026-10-27 18:21 UTC。阶段：代码已写、隔离验证通过；尚待主线统一构建、部署与真实只读联调。本轮没有访问上游，没有真实投注，没有修改生产配置，没有更改 TTL。

## 目标与边界

专家任务已经搜索并唯一绑定比赛后，允许业务侧请求一次该比赛的真实详情接口，再从数据库重读盘口；不能改为每条消息全站拉取，不能让接收时间代替上游 ctsp，不能在请求超时后自动再次获取价格。API 负责任务有效性、唯一绑定和事务外调用，collector 只接受已加载比赛的精确身份。

责任目录为采集服务与对应隔离测试。原控制服务和现有内部 Bearer token 被复用，不增加监听端口。没有修改 Telegram 客户端生命周期、MCP 权限或业务事务。

## 已实现合同

- POST 原控制服务的 /leyu/refresh，正文严格只有 fixture_id 与 source_match_id，额外字段拒绝。fixture_id 必须存在于采集内存投影，比赛必须是赛前或滚球；source_match_id 只接受对应的裸 mid 或投影中完全相同的 source_id。后者在现有协议中是 leyu:<mid>，最终详情请求始终使用已经核对的裸 mid，不接收任意 URL。
- 响应包含 fixture_id、source_match_id、严格布尔 accepted、written、整数 written_fixtures、脱敏 ASCII reason、retry_after_seconds。只有远端入库回执明确接受一场才返回 accepted=true；此字段仅证明写入，不能代替业务侧对源时间、状态与 TTL 的重读校验。
- 详情请求仅一次 POST 已有详情路径，复用原授权头、业务 Cookie、原始 origin、可配置 relay、大小限制、压缩解码与盘口解析；不发全站赛程或批量补盘请求，不使用续期登录代理，不尝试登录。
- runtime 复用原 collection_lock 的 FIFO 等待队列，精确请求在本次总截止内等待当前轮询释放，后续轮询不能插队；不取消现有轮询、不增加上游并发、不留后台预约。锁等待最多 8 秒；详情与入库的整体截止仍最多 8 秒，仍在等锁时返回 refresh_busy，源请求或入库超时返回 refresh_timeout，不重试价格；主线 MCP 的外层期限为 9 秒。
- 同比赛并发请求共享一次详情与一次入库，不同比赛已有刷新运行时返回 busy。控制接收端和真正上游详情发起点都限制全局及每源比赛至少 2 秒，避免慢配置读取压缩真正请求间隔。最后一个等待者取消时取消工作；一个等待者取消不破坏其他等待者。
- 轮询和精确刷新共享同一个上游 cooldown。详情 429/业务限流至少冷却 60 秒，并遵守 Retry-After 至多 900 秒；普通源异常进入有限退避。轮询的真实源异常、失败批次与限流也反向写入共享冷却。正常轮询 interval、纯数据陈旧、入库失败不伪装成上游限流。下一轮 poll 读取共享冷却，WS 补同步不能唤醒这段等待。
- 单场与全量路径共用单调合并：旧或未知源时间的 HTTP 报价不覆盖较新 WS 报价；保留同一报价完整的价格、状态及源时间。完整空盘口仍有真实删除语义；请求期间新 WS 盘口缺失于无水位的 HTTP 响应时标记不完整，避免误关闭。其他比赛的投影保持存在。

## 隔离验收与真实命令

初轮执行六模块回归，95 项行为通过但附加配置模块缺少 API 导入路径，进程退出 1；第二轮加入 API 路径后，附加配置模块又因缺少 tests 顶层路径导入失败，进程退出 1。这两次均属测试启动合同不全，未把失败标成通过。第三轮明确所有项目路径，158 项通过，退出 0。复核发现慢配置读取可能使实际详情起始间隔小于两秒，补实际发起点的门禁与行为测试后冻结最终版本。

最终命令使用 Docker 执行器：

    docker run --rm --name ai-football-match-refresh-final --network none --cpus .5 --memory 512m --pids-limit 96 --user 0:0 -i -e PYTHONDONTWRITEBYTECODE=1 -v /root/ai_football:/project:ro -v /root/ai_football/.runtime/quote-freshness-audit:/evidence:rw -w /tmp ai-football-verifier:v2.0.0-dev python -

容器 Python 入口先复制只读项目中的 services 与 tests 到 /tmp/frozen，冻结后才导入；PYTHONPATH 等价于 /tmp/frozen、/tmp/frozen/tests、/tmp/frozen/services、/tmp/frozen/services/api。执行 unittest 模块：tests.test_leyu_refresh、tests.test_leyu_snapshot_monotonic、tests.test_leyu_runtime、tests.test_leyu_protocol、tests.test_collector_lifecycle、tests.test_collector_quote_policy、tests.test_leyu_realtime、tests.test_telegram_collector。原始测试退出码显式传给容器进程，不由后续写摘要覆盖。

最终结果：159 项通过，失败 0、错误 0、跳过 0，unittest 4.070 秒，进程退出 0。刷新独立模块为 29 项。控制服务验收在 network none 容器本地回环真实 HTTP 验证 401、404、400、已有 status 路由、Bearer 鉴权及 no-store；其他请求仅 MockTransport/AsyncMock。机器摘要存于项目私有运行目录的 quote-freshness-audit/match-refresh-results.json，记录时间、模块、退出码及六项源码/测试 SHA-256。

新增测试覆盖单次详情、relay 与原头、压缩响应、未知源时间、重复或缺失详情、缺盘口容器、身份与 URL 注入、过期不续期、限流不重试、完整空快照、WS 乱序、其他比赛保留、锁忙、共享退避、实际发起间隔、相同比赛 singleflight、单/全部等待者取消、超时、错误入库回执、错误脱敏、原控制路由兼容、正常 interval 不阻塞刷新、入库失败不污染上游冷却及 WS 不能绕过共享冷却。

## 18:21 UTC 锁饥饿修复与测试时钟修正

主线报告两次独立真实探针得到 refresh_busy。本轮仅分析源码，没有再次发上游请求。原实现见 collection_lock 已占用就立即失败，没有排队资格；WS resync 可立即唤醒正常轮询等待，而 collect 持锁覆盖配置、全站 HTTP、补盘与合并，因此在持续补同步时精确请求可能一直错过短空窗。last_poll_at 与 elapsed 仅在整轮结束时更新，下一轮已开始时仍显示上一轮数据，不能据此判断现在是否空闲。

最小变更保留一个原 asyncio.Lock；精确请求进入其 FIFO 队列并最多等本次 8 秒，当前轮询正常结束后，已经排队的精确请求先于下一轮轮询运行。超过本次期限取消等待，不取消当前轮询，不保留后台任务。单纯锁等待超时不写入上游错误冷却；轮询期间新增的真实上游 cooldown 仍在获得锁后再次核验。未请求上游或未获入库回执时 accepted=false。新增 poll_in_progress、current_poll_started_at、current_poll_completed_at 区分正在运行与上一轮完成；主线负责 API 字段白名单。

有界复现还确认旧 health 测试将 wait_for_work 模拟为立即返回却不推进真实单调时钟，首个限流后的循环持续积累 AsyncMock 调用。测试外层只读 guard 在第 501 次冷却检查终止，仍剩 59.991 秒；这解释了全套日志 247 个已完成测试后的资源失控位置。该失败不是 Telegram 千条数据或冻结大资产导致。随后只修改该测试的共享假时钟及已读取默认 TTL 的前置状态，模拟等待推进 delay，生产冷却规则没有关闭；整个 health 模块 8 项通过，0.25 CPU/256 MiB 下最大 RSS 81,728 KiB，退出 0。

FIFO 补丁冻结后，Docker network none / 0.5 CPU / 384 MiB 执行刷新、health、runtime、单调快照、collector lifecycle、quote policy 六模块。90 项通过，失败/错误均 0，2.000 秒，最大 RSS 109,760 KiB，退出 0。新增真实顺序夹具证明 poll→refresh→next poll、等待超时不取消现 poll且无后台任务、现 poll 限流阻断排队详情、当前 poll 状态在运行与完成时准确变化。相关摘要为项目私有运行目录 quote-freshness-audit/fifo-refresh-results.json；原 159 项摘要对应 18:08 版本，不能冒充本次新源码的全套验收。

## 待主线验收

1. MCP 必须先核验任务租约和唯一比赛绑定，再在数据库事务外调用；收到写入成功也必须重读数据库并按真实源时间与现有 TTL 筛选，不能把刷新本身视为价格新鲜。
2. 主线统一构建后部署，使用授权只读详情执行一次真实联调，记录匹配标识、脱敏 reason、源时间变化及数据库投影，不输出会话值。
3. 上游返回的 ctsp 如果仍旧，系统应继续明确返回价格过期，不能因本功能已隔离通过就声明实时新鲜度恢复。
4. 未加载、结束、身份不一致、轮询忙、限流与超时保持未解决；不得自动放宽任务、TTL 或触发登录来换取成功。
