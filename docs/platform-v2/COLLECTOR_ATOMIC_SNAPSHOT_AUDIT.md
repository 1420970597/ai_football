# 采集运行告警与有界盘口分片修复

更新时间：2026-10-27 18:37 UTC。阶段：collector 代码已写、隔离验证通过；API 原子事务与可空盘口线由另一工作包统一修改，主线负责合并后的全套验证与部署。本文不声明当前生产已经恢复。

## 只读运行证据

采样只使用现有容器环境建立 read-only 数据库事务（5 秒语句期限），以及现有鉴权控制端 /status。没有调用上游、登录、续期、登出或投注。私有运行目录保存两个时间点聚合、有限日志及脚本；正文只展示计数、脱敏类别和哈希，不展示账户、群组身份、消息、令牌或 DSN。

18:28:07 UTC，Leyu poll 为 14 次、9 成功、4 失败，最新错误 fixture_quote_budget_exceeded。WS 已连接但 delivery_failures=43、queue_depth=128、dropped=3225，最后成功投递仍为 18:24:51，投递告警 active。当前 live active=2246、scheduled active=44307，15 秒内有效报价均为 0。Telegram 有两来源 HTTPStatusError，顶层 TimeoutError，四个租约三活跃。

18:31:23 UTC，WS 已恢复投递，delivery_failures=70、queue_depth=0、dropped=3569，18:30:25 告警恢复 clear。累计失败与丢弃不会因恢复清零；不能将 connected 当成全时段正常。live active=2307 且 fresh15=0；scheduled active=44307，fresh15=9、分布于两场。普通轮询仍可能重复出现单场超限，因此不能把这段短暂恢复写成稳定完成。

两个窗口相隔约 196 秒，四个持久检查点分别前进 4、3、34、2 条，合计 43 条，约 13.2 条/分钟；此为高水位推进量，包含未采用作者消息，不是推荐数量。窗口后四租约均活跃。历史审核新增一条 Hermes 结果；实时队列没有待处理或执行中记录，因此没有可据以估计实时审核吞吐的样本。待处理媒体 ready 从 885 增至 887，failed 从 16 降至 15，遗留失败类别为 TypeError；未复现其详情，不能臆断其根因。

有限 API 日志中，23 次 Telegram claim 409 全部位于 18:21:55–18:22:42；新 collector 启动于 18:21:47，与原有 90 秒源租约交接窗口吻合，之后没有新 409。另有三次 claim 503 在 18:28:31、18:31:26、18:31:37；唯一已记录的安全栈为 claim_checkpoint→source_for→fetchrow 的 TimeoutError，不能归因于 token、代理或上游 IP。fixtures 与 events 当时有大量 200，不支持宣称数据库整体不可用。后续仍需观测是否与热请求竞争或瞬态服务负载相关。

## 已确认缺陷

原 collector 在 fixture_batches 遇到任意单场超过 500 报价即抛异常；整个 poll 或同 WS 批次均被中止，WS 保留该批不断重试并产生队列拥塞。DB 没有单场超过 500 的记录，原因正是发送前拒绝；没有从运行中内存取得精确数量，不能用 DB 行数伪装上游当前盘口数量。

原 API 每场 markets 上限同为 500。简单扩大无限制列表，或者拆成多个 HTTP 请求并在最后一片标记 complete，都会分别带来无界内存/事务风险或错误关闭前片盘口。修复必须保留单场完整快照作为一个事务单位。

## 修复合同与边界

collector 对 0..500 报价保留普通 markets；501..2000 使用 market_chunks，每块最多 500，顶层显式 market_chunk_count 与 market_quote_count，删除 markets 键，markets_complete 保留顶层一次。所有块放在同一个 HTTP、同一个 fixture 中，不跨请求终结。API 工作包统一校验块数、总数、互斥、引用唯一、玩法线规范与整个 batch 最大 2000 报价，并在原事务内一次性应用全部报价与完整删盘。

collector 将同一批所有 fixture 的总报价限制为 2000；使用实际 UTF-8 编码后的 4 MiB 字节预算分批。预算针对完整序列化正文，不把字符数当作字节数。单场超过 2000 或单场正文本身超过 4 MiB 时保留内存原数据，仅隔离该场、不截断、不发送完整空盘、不冒充刷新成功；相邻比赛及后续 WS 继续投递。隔离以 fixture 哈希、数量及稳定类别记录，状态暴露最大观察数量，供后续用真实样本解释是否需要提高硬预算。正常未请求赔率的比赛仍发送不完整空 markets 的赛程信息，不将其内存盘口误报为待发送超限。

WS 投递失败新增稳定类别日志和 last_delivery_error_category；不写响应正文、请求地址或市场 payload。HTTPStatusError 等通用类别不代替进一步 API 日志定位。超预算隔离本身不再让整个 WS 消费者反复重试。

## 验证证据

命令使用 Docker verifier，network none、0.5 CPU、384 MiB、pids 64；只读挂载仓库，复制 services/tests 到 /tmp/frozen 后再导入，执行 quote_snapshot_chunks、leyu_refresh、leyu_health、collector_lifecycle、collector_quote_policy、leyu_realtime 六个模块。首轮 97 项通过；加入真实 API FixtureBatch 合同与缺末片拒绝后最终 98 项通过，0 失败、0 错误、退出 0，2.642 秒，最大 RSS 125732 KiB。机器证据为项目私有 quote-freshness-audit/atomic-chunk-collector-results.json，含所测 collector/test SHA-256。

专用测试证明：501/1001/2000 报价无丢失且每块有界；快照 complete 只在顶层；partial 不被提升；500 以下兼容；总 batch 与真实 UTF-8 字节预算；超硬预算不饿死相邻场次；同场所有片单次 HTTP；仅被隔离场不发请求且返回未写；未选赔率的完整赛程路径兼容；实际 API 接受 1001 条全部块、拒绝缺末片；投递异常类别不带 fixture payload。旧 lifecycle 的“501 必须抛错”断言改为“501 必须完整分片”，没有删除原无截断验收目的。

该离线轮次不包含临时 PostgreSQL 原子与回滚验收；后者由 API 工作包负责。需要合并后验证完整应用、末片缺失拒绝、乱序/重放、删盘与并发状态。不能用 collector 单测代替数据库证据。

## 后续验收

主线应先完成所需可空盘口线迁移与 API/collector 同版本部署，继续观察实际 max_observed_fixture_quotes、quarantined_fixtures、投递失败类别、队列深度与 15 秒 fresh 覆盖；在新鲜度低时保留真实过期状态。Telegram 90 秒交接 409 不应触发重新登录；持续 503 则继续按受限日志与数据库等待证据定位。运行中数据不做清理或回滚。
