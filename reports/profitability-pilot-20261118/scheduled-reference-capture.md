# 自然预检的同步外部报价采样

2026 年 11 月 19 日以近 72 小时已成交的赛事类型为起点，匹配同日赛程。东南亚球会锦标赛、欧女冠、巴甲各有一场已登记连续采样。每场从开球前 3 分钟开始，以 33 秒为轮次间隔，最多 238 轮。采集包括胜平负三项、全场大小、上半场大小和全场让球。欧女冠另有 Kalshi 全场大小与让球，巴甲另有 Kalshi 全场大小。 [柴桢对泰港目录](https://gamma-api.polymarket.com/events/slug/aseanc-svr-por-2026-11-19) [奥地利维也纳对拜仁目录](https://gamma-api.polymarket.com/events/slug/uwcl-faw-bm-2026-11-19) [瓦斯科对巴西国际目录](https://gamma-api.polymarket.com/events/slug/bra-vas-int-2026-11-19)

| 比赛 | 采样启动 UTC | 回放启动 UTC |
| --- | --- | --- |
| 柴桢对泰港 | 11 月 19 日 11:57 | 11 月 19 日 14:32 |
| 奥地利维也纳女足对拜仁女足 | 11 月 19 日 17:42 | 11 月 19 日 20:17 |
| 瓦斯科达伽马对巴西国际 | 11 月 19 日 22:27 | 11 月 20 日 01:02 |

03:28 UTC 欧女冠试采拿到三个 Polymarket 盘口的双边深度和各侧原生时间。Kalshi 的全场大小和让球均有双边深度，公开订单簿缺原生 UTC 时间。其成交时间仅适用于对应成交。三场计划使用的 21 个市场均经公开入口查验。公开目录可核验比赛和盘口含义，源独立性、滚球基线及实际交易费用仍需核实。 [Kalshi 欧女冠大小](https://api.elections.kalshi.com/trade-api/v2/markets?event_ticker=KXUCLWTOTAL-26NOV19FAKBMU) [Kalshi 巴甲大小](https://api.elections.kalshi.com/trade-api/v2/markets?event_ticker=KXBRASILEIROTOTAL-26NOV19VDGINT)

04:45 UTC 起验证每轮合并读取 Polymarket 订单簿。柴桢对泰港三轮各读 12 个选项，批量耗时 0.050 至 0.054 秒，逐项读取 0.644 至 0.707 秒。三轮返回顺序均不同，按市场条件和选项编号核对。04:48 UTC 欧女冠执行真实采集入口，一轮 0.847 秒拿到六个 Polymarket 市场的 12 份双边订单簿和各自原生时点，也拿到两份缺原生时间的 Kalshi 订单簿。Polymarket 同一批使用同一接收时点，原生报价仍使用各自上游时间。缺项、重复和截断留作采样失败。 [批量接口](https://docs.polymarket.com/api-reference/market-data/get-order-books-request-body)

05:25 UTC 用真实订单簿和诊断时点复核输出。03:28 UTC 归档中三组 Polymarket 报价的原生年龄为 50 至 89 秒，均被 30 秒门槛排除。新采集的柴桢对泰港六组均有完整深度，按各组响应后 1 毫秒作为诊断时点，三组通过时点检验并保留六份双边深度，三组超龄。诊断时点为人工构造，当期真实预检覆盖继续以赛后回放为准。

05:55 UTC 在回放中接入 21 个合约的选项映射。规则摘要与交易所接口、捕获档案及私有赛事绑定一致时，回放以核验过的原生名称确定主客方向和大小球两侧。胜平负要求同一来源有三个方向各自的 Yes 和 No，二元盘口要求同一合约有两侧。重复方向、缺项和名称变更有独立状态。按最新批次完成后 1 毫秒的诊断时点，欧女冠有胜平负、半场大小和让球三个完整组，巴甲有让球组。该时点用于验证回放，真实样本仍按预检记录。组齐备后还需要核验来源独立性、跨源原生时间差、滚球状态及实际费用。 [方向清单](reference-outcomes-20261119.json) [公开欧女冠目录](https://gamma-api.polymarket.com/events/slug/uwcl-faw-bm-2026-11-19) [公开巴甲目录](https://gamma-api.polymarket.com/events/slug/bra-vas-int-2026-11-19)

06:03 UTC，新增 Polymarket 开球时段流采样，三个窗口从 12:00、17:45 和 22:30 UTC 开始，各观察 15 分钟。档案记录每个合约的规则摘要和选项编号、原始帧、原生时间及本机实际接收时间。首份订单簿单独标识，重连初始快照沿用上游时间。欧女冠实测 50 秒收到 56 条变动，接收延迟为 10.6 至 57.1 毫秒。独立短测保存 12 份初始订单簿、19 条增量和正常结束标志。流用于测定覆盖、延迟及变化时点。赛后预检仍使用有完整双边深度的 REST 档案。中断保留已收到帧，缺正常结束标志时按不完整档案处理。 [采样程序](reference_orderbook_stream.py) [官方公开流](https://docs.polymarket.com/market-data/realtime-data#market-stream)

瓦斯科额外从 22:27 UTC 在同一轮采集两家来源的全场大小 2.5 和 3.5，用于检查四分之一线的相邻报价覆盖。该采样单列为 `vasco-adjacent-totals`，赛后与同场主档案一起回放。两条线分别选预检前最新记录，同一来源每条线都须有完整且有效的双边深度，四侧原生时差最多 5 秒。回放保留各线缺项、停盘、规则或名称变化，报价齐备只表示有同期深度。进入定价还需要核验完赛及异常赛果条款、来源独立性、滚球比分及实际费用。06:09 至 06:11 UTC 的受理样本发生在采样窗口之前。 [Polymarket 盘口目录](https://gamma-api.polymarket.com/events/slug/bra-vas-int-2026-11-19-more-markets) [Kalshi 大小球目录](https://api.elections.kalshi.com/trade-api/v2/markets?event_ticker=KXBRASILEIROTOTAL-26NOV19VDGINT)

采样保存规则摘要、原生报价和接收时点。赛后任务按赛事身份、期间、玩法和盘口线，将同一笔已验证预检与各候选市场的最近一轮完整报价配对。原生时点和接收时点都须落在预检前 30 秒内。通过时保留规则摘要、原生选项名称、买卖价及深度和各侧原生及接收时点，用于复算；外部源独立性及同期滚球状态仍需校验。最新观测停盘、缺深度、原生时点不可核验或规则变化时，输出相应原因。完全无预检的比赛保留采样轮数。规则摘要变更需重新核验。 [采样入口](../../infra/profitability/capture_reference_window.sh) [生产预检只读查询](../../infra/profitability/reference_preflights.sql) [回放入口](../../infra/profitability/replay_reference_window.sh)

部署使用临时 systemd 定时器。主机重启后运行 `bash infra/profitability/restore_reference_pilot.sh`。入口复用当期私有绑定档案，保留已经运行的定时器。未开始的赛程仍按原时间采样，进行中的赛程从当前时间采集剩余轮次，过去的时段保留缺口。原窗口已闭合时，只为有采样且缺少结果的比赛补做回放。回放合并重启前后的完整记录，遇到未写完的尾行时记录中断档案数，档案内已换行的坏行会使回放失败。Docker 回放复现两场此前巴甲的 5 条真实预检。欧女冠提前试运行生成 1 轮订单簿、0 笔预检的归档。脚本仅观察公开市场和已写入的执行记录。 [恢复入口](../../infra/profitability/restore_reference_pilot.sh) [回放边界测试](test_reference_preflight.py)

10:17 UTC，生产预检回放加入原生赛事项目、盘口编号、已核验的结算范围和盘口方向。足球赛前全期间报价还需逐条核对两边的结算条款。滚球全期间盘口需要独立记录的同一比分和比赛状态。剩余期间盘口需要按对应比分基线结算的外部合约。球队大小按原生盘口编号核对主队或客队。状态单列为 `settlement_comparison`，仍保留原报价时效和深度状态。68 项 Docker 研究测试通过。对瓦斯科两笔真实预检回放，赛前全场大小 2.75 和主队视角 −0.75 均标为待核对结算条款，采样在两笔预检之后，仍缺同期外部报价。 [结算范围规则](../../services/api/app/score_rules.py) [回放实现](reference_preflight.py)

10:56 UTC，已确认 ESPN 摘要与现有两个比赛绑定的主客、赛事及开球时间一致。欧女冠和瓦斯科的赛况分别从 17:42 和 22:27 UTC 每 33 秒取样，与同场订单簿使用相同起点。赛前两次线上读取成功，赛事尚未开始，逐事件为空。此前两场同联赛历史摘要的 66 条关键事件均带原生 UTC 时点，比赛状态摘要无原生更新时间。采样保留原生事件时点以及本机请求、接收时间，同一事件后续更正逐轮留存。红牌和 VAR 的完整性、数据供应商独立性以及现场延迟仍待验证。柴桢场保留原订单簿采集，尚无经核验的第三方状态流。 [UEFA 官方赛程](https://www.uefa.com/womenschampionsleague/match/2050573/) [ESPN 欧女冠摘要](https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.wchampions/summary?event=401916978) [ESPN 巴甲摘要](https://site.api.espn.com/apis/site/v2/sports/soccer/bra.1/summary?event=401841308) [采样与恢复入口](../../infra/profitability/restore_reference_pilot.sh)
