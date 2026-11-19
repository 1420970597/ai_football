# 自然预检的同步外部报价采样

## 按历史实际下单时段验证

11 月 19 日 20:45 UTC 起，在生产 API 容器以只读事务统计近 48 小时有同次预检和原生订单号的自动单。欧女冠 14 笔赛前单中，开球前 150 分钟以内有 1 笔，前 330 分钟内有 8 笔。巴甲 5 笔和 MLS 附加赛 4 笔均在开球前 8 小时以上。巴甲、MLS 共有 6 笔在 11 月 19 日 06:09 至 06:11 UTC 受理，巴甲另 3 笔在 11 月 18 日 11:51 至 14:09 UTC 受理。这两组时间用于选择采样时段，后续实际受理情况独立统计。

| 比赛 | 开球 UTC | 额外采样 UTC |
| --- | --- | --- |
| 拜仁慕尼黑对科隆 | 11 月 20 日 19:30 | 11 月 20 日 05:58 至 06:25 |
| 帕尔梅拉斯对弗拉门戈 | 11 月 21 日 19:30 | 11 月 21 日 05:58 至 06:25、11:45 至 14:12 |
| 巴伊亚对科里蒂巴 | 11 月 21 日 20:30 | 11 月 21 日 05:58 至 06:25、11:45 至 14:12 |
| 新英格兰革命对费城联合 | 11 月 21 日 19:00 | 11 月 21 日 05:58 至 06:25 |

四场均以官方赛程和乐鱼记录核对主客和开球，并用 `expand_reference_catalog.py` 生成半球盘口绑定。11 月 21 日三场的单轮采集各返回 44 份盘口记录，拜仁场已通过盘口目录映射。6 个采集时段及 6 个回放定时器已启用。每轮间隔 10 秒，单个容器上限为 0.25 CPU 和 128 MiB。回放在各时段结束后 75 秒运行，以 `--preview` 隔离早段和标准时段归档。宿主机重启后按表恢复未过期的临时单元。 [德甲赛程](https://products.bundesliga.com/fixtures) [巴甲赛程](https://credencial.cbf.com.br/competicoes/listar/42/1) [MLS 官方赛程](https://bracket.mlssoccer.com/news/mls-announces-schedule-for-audi-2026-mls-cup-playoffs-round-one-best-of-3-series)

Kalshi 两场挂牌全场大 2.5 的盘口 REST 响应均无原生报价时点，市场详情的 `updated_time` 均停在 11 月 14 日。独立择价仍需第二来源符合原生时间约束，并核实同合同规则与成本。随自然预检回放，逐笔保留价格、可用深度、实际回执以及跟单、达到最低赔率才下单和不下单三种结果。 [帕尔梅拉斯合约](https://kalshi.com/markets/kxbrasileirototal/brasileiro-total/kxbrasileirototal-26nov21palfla) [新英格兰合约](https://kalshi.com/markets/kxmlstotal/mls-total/kxmlstotal-26nov21nephi) [市场详情字段](https://docs.kalshi.com/api-reference/market/get-market)

## 原采样档案

2026 年 11 月 19 日以近期已成交的赛事类型为起点，匹配同日赛程。东南亚球会锦标赛和巴甲各登记一场连续采样，欧女冠登记两场。每场从开球前 3 分钟开始，以 33 秒为轮次间隔，最多 238 轮。采集包括胜平负三项、全场大小、上半场大小和全场让球。奥地利维也纳对拜仁另有 Kalshi 全场大小与让球，巴甲另有 Kalshi 全场大小。 [柴桢对泰港目录](https://gamma-api.polymarket.com/events/slug/aseanc-svr-por-2026-11-19) [奥地利维也纳对拜仁目录](https://gamma-api.polymarket.com/events/slug/uwcl-faw-bm-2026-11-19) [本菲卡对罗马目录](https://gamma-api.polymarket.com/events/slug/uwcl-slb-asr-2026-11-19) [瓦斯科对巴西国际目录](https://gamma-api.polymarket.com/events/slug/bra-vas-int-2026-11-19)

| 比赛 | 采样启动 UTC | 回放启动 UTC |
| --- | --- | --- |
| 柴桢对泰港 | 11 月 19 日 11:57 | 11 月 19 日 14:32 |
| 奥地利维也纳女足对拜仁女足 | 11 月 19 日 17:42 | 11 月 19 日 20:17 |
| 本菲卡女足对罗马女足 | 11 月 19 日 19:57 | 11 月 19 日 22:32 |
| 瓦斯科达伽马对巴西国际 | 11 月 19 日 22:27 | 11 月 20 日 01:02 |

16:57 UTC，维也纳、本菲卡和瓦斯科三个已核验盘口目录增加单独的赛前采样，目标区间为开球前 150 至 3 分钟，主盘每 40 秒，扩展盘每 10 秒。维也纳两个实际采集进程于 16:53:08 UTC 开始，归档的主盘有 12 份带原生时点的 Polymarket 双边订单簿，另有 2 份缺原生时点的 Kalshi 双边订单簿。扩展盘 41 个合约的最新已检轮次返回 64 份有双边和原生时点的订单簿，另有 18 份缺一侧。本菲卡和瓦斯科的新采样分别定于 17:30、20:00 UTC。原开球前三分钟起的采样和盘口流继续运行。已更新三场的正式回放参数，按实际归档时间判断每笔真实预检的报价覆盖。 [采样恢复入口](../../infra/profitability/restore_reference_pilot.sh)

固定生产只读样本为 11 月 17 日 16:54:53 至 19 日 16:54:53 UTC，按预检时间筛选实际受理自动订单。48 笔赛前成交、33 场，开球前 3 分钟内 0 笔，前 3 至 60 分钟 3 笔，前 60 至 150 分钟 5 笔，更早 40 笔。中位 322.14 分钟，P90 为 1,589.04 分钟。分布仅衡量时间覆盖。三场的真实同合同、双来源时点、深度、成本和费后结果由正式回放逐笔定性。 [生产预检查询](../../infra/profitability/reference_preflights.sql)

17:38 UTC，本菲卡和瓦斯科增加 Polymarket US 的三项正常时间胜平负观察。本人订单仍只读。官方赛事、对阵、开球、选项问题和规则摘要逐份对齐，六份订单簿都返回有效双边深度和原生 `transactTime`。本菲卡主盘、扩展盘按原定于 17:30 UTC 启动，美国市场补充采集于 17:36:03 UTC 启动，并已登记两场赛前及开球后的恢复任务。取本菲卡美国市场两轮真实采样完成时点各加一毫秒做诊断，其中一轮美国市场三项都有 30 秒内双边报价，国际市场两轮均未配齐。该诊断不等同于真实预检。17:37 UTC 生产预览实际读取 57 轮、自然预检 0。回放按已核验规则摘要记录独立的原生 YES 买卖盘覆盖，保留深度与时点，也保留来源独立性及异常赛果条款的验证缺口。 [本菲卡赛事](https://gateway.polymarket.us/v1/events/slug/uwcl-slb-asr-2026-11-19) [瓦斯科赛事](https://gateway.polymarket.us/v1/events/slug/bra-vas-int-2026-11-19) [美国盘口接口](https://docs.polymarket.us/api-reference/markets/get-market-book)

13:48 UTC，近 48 小时实际订单中，东南亚球会锦标赛、巴甲、欧女冠分别有 14、13、10 笔。欧女冠新增一场，采样时段与已有三场错开。生产赛事、欧足联赛程、ESPN 赛况和 Polymarket 原生开球时间相符。单轮线上试采读取六个市场的 12 份订单簿，规则摘要均与采样映射和私有赛事绑定一致。ESPN 试读取回正确的主客标识和赛前状态。已登记 19:57 UTC 报价及比分采样、20:00 UTC 盘口公开流和 22:32 UTC 回放。恢复依赖宿主取证目录中的原始赛事绑定，其中已加入该场六个来源绑定。 [欧足联赛程](https://www.uefa.com/womenschampionsleague/news/02ab-220ff9af21a9-748c51d8b995-1000--women-s-champions-league-matchday-5-preview-chelsea-vs-pa/) [ESPN 赛况](https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.wchampions/summary?event=401916974) [方向映射](reference-outcomes-20261119.json)

14:35 UTC，柴桢对泰港的 238 轮外部档案与一笔自然预检进入独立价格规则。13:29:07.703435 UTC 的实际预检为滚球全场大 3.5、赔率 1.58，已受理并结算，场馆回执 +2.90，费用和币种仍缺。同期档案的全场大小只覆盖 2.5。公开目录有 3.5，赛后页面和成交数据无法证明预检时的双边可成交深度。本场也无完整的独立现场状态。结果为现行策略 1 笔、价格规则 0 笔、观望 0 笔，三组没有可比的费后净额。真实订单和既定账户许可保持原有流程。 [场馆目录](https://polymarket.com/sports/aseanc-games/aseanc-svr-por-2026-11-19) [价格规则](preflight_price_decision.py)

14:57 UTC，已为维也纳对拜仁、本菲卡对罗马、瓦斯科对巴西国际登记 41、41、40 个额外 Polymarket 合约，覆盖全场让球、半场及全场大小、半场及全场各队大小。各场沿用原定开始时点及回放。采样每轮读取一次赛事目录，按不超过 16 个选项分批取订单簿。识别主客、开球、盘口线后生成的私有 `catalog-<赛事标签>.json` 保存方向及规则摘要，恢复入口沿用同一份清单。三场用同一入口试采，244 份订单簿全部返回，规则摘要均匹配。按当轮最终接收时点构造的诊断，分别有 14、9、18 个合约达到双边深度及时效。Kalshi 的原生时间盘口流仍需可用 API 凭据。 [维也纳附加盘](https://gamma-api.polymarket.com/events/slug/uwcl-faw-bm-2026-11-19-more-markets) [本菲卡附加盘](https://gamma-api.polymarket.com/events/slug/uwcl-slb-asr-2026-11-19-more-markets) [瓦斯科附加盘](https://gamma-api.polymarket.com/events/slug/bra-vas-int-2026-11-19-more-markets) [Kalshi 订单簿流](https://docs.kalshi.com/websockets/orderbook-updates)

15:10 UTC，赛况采集保留按原生球队编号绑定的红牌统计与逐事件归属，回放标注同源统计与事件一致、冲突或缺失，并检查订单簿是否早于最近已知的进球、红牌、正常比赛阶段边界或暂停事件。缺原生时点及未解除暂停阻止滚球定价资格。历史欧女冠正式样本有 45 条关键事件，客队一张红牌的原生时间为 11 月 18 日 18:27:37 UTC，球队统计和逐事件均为主队 0、客队 1。今晚三场赛前摘要均缺红牌统计，程序保留未知。球队红牌两条证据来自同一服务，乐鱼独立状态、完整 VAR 及恢复时点仍待核验。 [欧女冠原生样本](https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.wchampions/summary?event=401916981)

15:22 UTC，三个扩展盘口采样改为每 5 秒，仍在原定时段内，维也纳 41 个合约真实试采七轮全部返回，实际间隔 4.946 至 5.097 秒。按现有采样调用，扩展采集每场每十秒约 2 次赛事目录和最多 12 次批量订单簿，低于两类公开限额。回放逐行保留每个市场在每次真实预检之前最近的观察，包括开放、停盘或失败，同时记录全档轮数。用真实试采行构造的 1,565 轮压力样本在生产回放资源限制下耗时 2.10 秒、峰值 28,452 KiB。真实柴桢档案复跑仍为 238 轮与 1 笔自然预检。采样频次不能补外部报价原生发布时间、场馆费用或完整现场状态。 [公开限额](https://docs.polymarket.com/api-reference/rate-limits) [采样恢复入口](../../infra/profitability/restore_reference_pilot.sh)

03:28 UTC 欧女冠试采拿到三个 Polymarket 盘口的双边深度和各侧原生时间。Kalshi 的全场大小和让球均有双边深度，公开订单簿缺原生 UTC 时间。其成交时间仅适用于对应成交。三场计划使用的 21 个市场均经公开入口查验。公开目录可核验比赛和盘口含义，源独立性、滚球基线及实际交易费用仍需核实。 [Kalshi 欧女冠大小](https://api.elections.kalshi.com/trade-api/v2/markets?event_ticker=KXUCLWTOTAL-26NOV19FAKBMU) [Kalshi 巴甲大小](https://api.elections.kalshi.com/trade-api/v2/markets?event_ticker=KXBRASILEIROTOTAL-26NOV19VDGINT)

04:45 UTC 起验证每轮合并读取 Polymarket 订单簿。柴桢对泰港三轮各读 12 个选项，批量耗时 0.050 至 0.054 秒，逐项读取 0.644 至 0.707 秒。三轮返回顺序均不同，按市场条件和选项编号核对。04:48 UTC 欧女冠执行真实采集入口，一轮 0.847 秒拿到六个 Polymarket 市场的 12 份双边订单簿和各自原生时点，也拿到两份缺原生时间的 Kalshi 订单簿。Polymarket 同一批使用同一接收时点，原生报价仍使用各自上游时间。缺项、重复和截断留作采样失败。 [批量接口](https://docs.polymarket.com/api-reference/market-data/get-order-books-request-body)

05:25 UTC 用真实订单簿和诊断时点复核输出。03:28 UTC 归档中三组 Polymarket 报价的原生年龄为 50 至 89 秒，均被 30 秒门槛排除。新采集的柴桢对泰港六组均有完整深度，按各组响应后 1 毫秒作为诊断时点，三组通过时点检验并保留六份双边深度，三组超龄。诊断时点为人工构造，当期真实预检覆盖继续以赛后回放为准。

05:55 UTC 在回放中接入 21 个合约的选项映射。规则摘要与交易所接口、捕获档案及私有赛事绑定一致时，回放以核验过的原生名称确定主客方向和大小球两侧。胜平负要求同一来源有三个方向各自的 Yes 和 No，二元盘口要求同一合约有两侧。重复方向、缺项和名称变更有独立状态。按最新批次完成后 1 毫秒的诊断时点，欧女冠有胜平负、半场大小和让球三个完整组，巴甲有让球组。该时点用于验证回放，真实样本仍按预检记录。组齐备后还需要核验来源独立性、跨源原生时间差、滚球状态及实际费用。 [方向清单](reference-outcomes-20261119.json) [公开欧女冠目录](https://gamma-api.polymarket.com/events/slug/uwcl-faw-bm-2026-11-19) [公开巴甲目录](https://gamma-api.polymarket.com/events/slug/bra-vas-int-2026-11-19)

06:03 UTC，新增 Polymarket 开球时段流采样，三个窗口从 12:00、17:45 和 22:30 UTC 开始，各观察 15 分钟。档案记录每个合约的规则摘要和选项编号、原始帧、原生时间及本机实际接收时间。首份订单簿单独标识，重连初始快照沿用上游时间。欧女冠实测 50 秒收到 56 条变动，接收延迟为 10.6 至 57.1 毫秒。独立短测保存 12 份初始订单簿、19 条增量和正常结束标志。流用于测定覆盖、延迟及变化时点。赛后预检仍使用有完整双边深度的 REST 档案。中断保留已收到帧，缺正常结束标志时按不完整档案处理。 [采样程序](reference_orderbook_stream.py) [官方公开流](https://docs.polymarket.com/market-data/realtime-data#market-stream)

瓦斯科额外从 22:27 UTC 在同一轮采集两家来源的全场大小 2.5 和 3.5，用于检查四分之一线的相邻报价覆盖。该采样单列为 `vasco-adjacent-totals`，赛后与同场主档案一起回放。两条线分别选预检前最新记录，同一来源每条线都须有完整且有效的双边深度，四侧原生时差最多 5 秒。回放保留各线缺项、停盘、规则或名称变化，报价齐备只表示有同期深度。进入定价还需要核验完赛及异常赛果条款、来源独立性、滚球比分及实际费用。06:09 至 06:11 UTC 的受理样本发生在采样窗口之前。 [Polymarket 盘口目录](https://gamma-api.polymarket.com/events/slug/bra-vas-int-2026-11-19-more-markets) [Kalshi 大小球目录](https://api.elections.kalshi.com/trade-api/v2/markets?event_ticker=KXBRASILEIROTOTAL-26NOV19VDGINT)

采样保存规则摘要、原生报价和接收时点。赛后任务按赛事身份、期间、玩法和盘口线，将同一笔已验证预检与各候选市场的最近一轮完整报价配对。原生时点和接收时点都须落在预检前 30 秒内。通过时保留规则摘要、原生选项名称、买卖价及深度和各侧原生及接收时点，用于复算；外部源独立性及同期滚球状态仍需校验。最新观测停盘、缺深度、原生时点不可核验或规则变化时，输出相应原因。完全无预检的比赛保留采样轮数。规则摘要变更需重新核验。 [采样入口](../../infra/profitability/capture_reference_window.sh) [生产预检只读查询](../../infra/profitability/reference_preflights.sql) [回放入口](../../infra/profitability/replay_reference_window.sh)

部署使用临时 systemd 定时器。主机重启后运行 `bash infra/profitability/restore_reference_pilot.sh`。入口复用当期私有绑定档案，保留已经运行的定时器。未开始的赛程仍按原时间采样，进行中的赛程从当前时间采集剩余轮次，过去的时段保留缺口。原窗口已闭合时，只为有采样且缺少结果的比赛补做回放。回放合并重启前后的完整记录，遇到未写完的尾行时记录中断档案数，档案内已换行的坏行会使回放失败。Docker 回放复现两场此前巴甲的 5 条真实预检。欧女冠提前试运行生成 1 轮订单簿、0 笔预检的归档。脚本仅观察公开市场和已写入的执行记录。 [恢复入口](../../infra/profitability/restore_reference_pilot.sh) [回放边界测试](test_reference_preflight.py)

11 月 19 日 19:53 UTC，按未来赛程再登记五场。先用正式赛程核对双方、主客和开球，以 `expand_reference_catalog.py` 写入带独立原生赛事、盘口线和规则摘要的私有目录。保存为 `catalog-<目录标签>.json` 后，运行 `bash infra/profitability/restore_reference_pilot.sh --schedule-catalog <目录标签> <乐鱼赛事编号> <UTC 开球时间>`。重启后使用相同参数恢复该场。赛前、滚球、回放分别在开球前 150 分钟、前 3 分钟和后 152 分钟预约。

| 比赛 | 开球 UTC | 对应目录 |
| --- | --- | --- |
| 维多利亚对布拉甘蒂诺 | 11 月 19 日 22:30 | `bra-vit-bra-2026-11-19` |
| 圣保罗对弗鲁米嫩塞 | 11 月 20 日 00:30 | `bra-sao-flu-2026-11-19` |
| 奥兰多城对辛辛那提 | 11 月 20 日 00:30 | `mls-orl-fcc-2026-11-19` |
| 西雅图海湾人对皇家盐湖城 | 11 月 20 日 03:00 | `mls-sea-rsl-2026-11-19` |
| 西悉尼流浪者对阿德莱德联 | 11 月 20 日 08:40 | `aus-wsw-ade-2026-11-20` |

五场采用同一只读报价源，匹配到全场让球、全场大小、半场大小和部分球队大小。MLS 的晋级和点球合同在本轮绑定范围之外，正常时间及异常情形仍需逐盘核对。同期第二源、合法费用和实际预检均由回放独立核验。 [巴甲赛程](https://credencial.cbf.com.br/competicoes/listar/42/1) [MLS 赛程及赛制](https://www.mlssoccer.com/playoffs/2026/news/wild-card-format-key-info-audi-2026-mls-cup-playoffs) [澳超赛程](https://aleagues.com.au/news/news-aleague-men-ins-outs-matchweek-5/)

10:17 UTC，生产预检回放加入原生赛事项目、盘口编号、已核验的结算范围和盘口方向。足球赛前全期间报价还需逐条核对两边的结算条款。滚球全期间盘口需要独立记录的同一比分和比赛状态。剩余期间盘口需要按对应比分基线结算的外部合约。球队大小按原生盘口编号核对主队或客队。状态单列为 `settlement_comparison`，仍保留原报价时效和深度状态。68 项 Docker 研究测试通过。对瓦斯科两笔真实预检回放，赛前全场大小 2.75 和主队视角 −0.75 均标为待核对结算条款，采样在两笔预检之后，仍缺同期外部报价。 [结算范围规则](../../services/api/app/score_rules.py) [回放实现](reference_preflight.py)

10:56 UTC，已确认 ESPN 摘要与现有两个比赛绑定的主客、赛事及开球时间一致。欧女冠和瓦斯科的赛况分别从 17:42 和 22:27 UTC 每 33 秒取样，与同场订单簿使用相同起点。赛前两次线上读取成功，赛事尚未开始，逐事件为空。此前两场同联赛历史摘要的 66 条关键事件均带原生 UTC 时点，比赛状态摘要无原生更新时间。采样保留原生事件时点以及本机请求、接收时间，同一事件后续更正逐轮留存。红牌和 VAR 的完整性、数据供应商独立性以及现场延迟仍待验证。柴桢场保留原订单簿采集，尚无经核验的第三方状态流。 [UEFA 官方赛程](https://www.uefa.com/womenschampionsleague/match/2050573/) [ESPN 欧女冠摘要](https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.wchampions/summary?event=401916978) [ESPN 巴甲摘要](https://site.api.espn.com/apis/site/v2/sports/soccer/bra.1/summary?event=401841308) [采样与恢复入口](../../infra/profitability/restore_reference_pilot.sh)

11:37 UTC，回放开始为已绑定的足球滚球预检读取同场 ESPN 档案。选取预检前最新一份接收记录，要求接收距预检最多 45 秒，校对原生赛事、联赛、主客、开球和正在比赛状态。报告按笔保存比分一致、比分冲突、缺比分、错阶段、超时和错误，附外部显示比赛时钟和最新有效原生事件时点。仍需另核红牌、VAR、原生状态更新时间及供应商独立性。70 项隔离测试通过，瓦斯科早场两笔真实预检成功复跑，两份真实赛前档案也通过拒绝其参与滚球比较的探测。 [回放入口](../../infra/profitability/replay_reference_window.sh) [赛况绑定](reference-score-bindings-20261119.json)

12:07 UTC，柴桢开球流触发原 16 MiB 上限，保存前 19,033 条价格变化及 10,808 条首档变动，末帧接收于 12:07:45.882 UTC。恢复服务于 12:08:29 UTC 启动，新档案的初始盘口于 12:08:31.267 UTC 到达，和原档案相隔 45.385 秒。恢复段保留原定 12:15 UTC 截止。采集器单档上限调整为 64 MiB，并保留 1 MiB 的单帧限制。71 项研究测试和 5 项恢复测试通过，公开短测核验了绝对截止。 [采集器](reference_orderbook_stream.py) [恢复入口](../../infra/profitability/restore_reference_pilot.sh)

12:35 UTC，回放逐段读取市场流，只使用预检时点前收到而且原生报价未超龄的同段双侧首档。只有时点落在已见记录之间才输出流覆盖。每段核对市场和选项身份、合约条款摘要、原生及接收时间，另报告是否正常结束。柴桢两段真实档案在三个诊断时点对照成功。12:05:04 的首档与 REST 不同，12:08:00 落在中断间隔，12:10:18 的价格一致。两个有流覆盖的时点，流报价年龄分别为 4.260 秒和 0.489 秒，REST 分别为 20.835 秒和 8.533 秒。三个诊断时点为人工选取，生产截至 12:30 UTC 仍无同场预检。流首档只包含价格，成交深度和开盘状态继续从原盘口档案核验。 [市场流文档](https://docs.polymarket.com/market-data/realtime-data#market-stream) [流回放](reference_stream_replay.py)
