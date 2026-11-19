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

采样保存规则摘要、原生报价和接收时点。赛后任务按赛事身份、期间、玩法和盘口线，将同一笔已验证预检与各候选市场的最近一轮完整报价配对。原生时点和接收时点都须落在预检前 30 秒内。通过时保留规则摘要、原生选项名称、买卖价及深度和各侧原生及接收时点，用于复算；外部源独立性及同期滚球状态仍需校验。最新观测停盘、缺深度、原生时点不可核验或规则变化时，输出相应原因。完全无预检的比赛保留采样轮数。规则摘要变更需重新核验。 [采样入口](../../infra/profitability/capture_reference_window.sh) [生产预检只读查询](../../infra/profitability/reference_preflights.sql) [回放入口](../../infra/profitability/replay_reference_window.sh)

部署使用临时 systemd 定时器。主机重启后运行 `bash infra/profitability/restore_reference_pilot.sh`。入口复用当期私有绑定档案，保留已经运行的定时器。未开始的赛程仍按原时间采样，进行中的赛程从当前时间采集剩余轮次，过去的时段保留缺口。原窗口已闭合时，只为有采样且缺少结果的比赛补做回放。回放合并重启前后的完整记录，遇到未写完的尾行时记录中断档案数，档案内已换行的坏行会使回放失败。Docker 回放复现两场此前巴甲的 5 条真实预检。欧女冠提前试运行生成 1 轮订单簿、0 笔预检的归档。脚本仅观察公开市场和已写入的执行记录。 [恢复入口](../../infra/profitability/restore_reference_pilot.sh) [回放边界测试](test_reference_preflight.py)
