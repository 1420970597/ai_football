# 巴甲自然成交的外部报价覆盖

2026 年 11 月 18 日 23:17 至 23:24 UTC 使用公开只读接口，复核同一账户三笔已有乐鱼受理的赛前订单。按真实下单时点向前查十分钟。三场的球队、赛种和开球时间均与 Polymarket 赛事记录吻合，Kalshi 巴甲目录和合约规则也列出相同对阵与比赛日。两家正常完赛的结算按 90 分钟加补时。取消、延期或中断另有规则，以下推算仅覆盖正常完赛。 [卡尔希分钟报价文档](https://docs.kalshi.com/api-reference/market/get-market-candlesticks) [Polymarket 赛事接口](https://docs.polymarket.com/developers/gamma-markets-api/get-events)

| 已有订单及实际成交价 | Polymarket 历史点 | Kalshi 共同分钟 |
| --- | --- | --- |
| 雷默对沙佩科人全场大 2.75，1.85 | 大 2.5 为 0.565，大 3.5 为 0.335，距成交约 37 至 40 秒 | 大 2.5 无有效分钟报价，无共同分钟 |
| 克鲁塞罗对帕尔梅拉斯客队平手，1.85 | 主、平、客为 0.355、0.265、0.375，距成交约 25 至 28 秒 | 14:05 UTC 三项齐全，距成交约 280 秒 |
| 弗拉门戈对巴拉纳竞技客队受让 +2，1.90 | 主队赢至少 2 球及至少 3 球的历史点为 0.61、0.39，距成交约 55 至 57 秒 | 12:06 UTC 两项收盘买卖价为 0.61–0.62、0.38–0.40，距成交约 10 秒 |

按 Polymarket 历史点估计正常完赛、未计成本的赔率分界，三单依表顺序约为 1.967、1.947、2.000。全场大 2.75 在恰好三球时半赢，平手盘排除平局，客队 +2 在主队恰好赢两球时走盘。对第三单，令 `L` 为主队赢至少两球的概率，`H` 为主队赢至少三球的概率，客队 +2 的分界为 `1 + H / (1 - L)`。按 Kalshi 这两条历史买卖区间复算约为 1.974 至 2.053。三个实际成交价都低于对应历史点的分界。该估计依赖市场价格所隐含的概率，历史点和分钟柱均未提供对应时刻的成交深度。 [雷默相邻大小线](https://gamma-api.polymarket.com/events/slug/bra-cre-cha-2026-11-18-more-markets) [帕尔梅拉斯胜平负三项](https://gamma-api.polymarket.com/events/slug/bra-cru-pal-2026-11-18) [弗拉门戈相邻让球线](https://gamma-api.polymarket.com/events/slug/bra-fla-cap-2026-11-18-more-markets) [Kalshi 弗拉门戈让球市场](https://api.elections.kalshi.com/trade-api/v2/markets?event_ticker=KXBRASILEIROSPREAD-26NOV18FLACAP)

23:21 UTC 的当前订单簿抽查中，Polymarket 四个抽样市场的两侧均有深度及原生快照时间。Kalshi 同四个抽样市场均返回双边买盘，响应体缺少原生报价时点。该次采样发生在旧单成交数小时后。后续候选需保存来源自身的盘口时点、同合同的非空深度及收费和状态证据，并对真实预检时点配对。仅本轮第三单具备两个市场较接近的历史记录，仍没有第二个独立、同步、含实际深度的历史报价样本可用于评估成交规则。 [Kalshi 当前订单簿文档](https://docs.kalshi.com/api-reference/market/get-market-orderbook)

## 开球前后订单簿试采

11 月 18 日 23:29:49 至 23:33:21 UTC 完成两批、八轮采样。采样器保存市场与选项身份、规则摘要、实际接收时间、最优买卖价及各自规模。Polymarket 订单簿同时保存来源时点。Kalshi 按 YES 买盘与 NO 买盘的补集还原 YES 买卖边，源时点保留为空。市场元数据中的更新时间和订单簿接收时间也无法补足这一缺口。 [Polymarket 盘口数据](https://docs.polymarket.com/market-data/prices-order-books) [Kalshi 当前订单簿文档](https://docs.kalshi.com/api-reference/market/get-market-orderbook)

| 盘口组合 | 轮次 | Polymarket 双边齐全且有原生时点 | Polymarket 同轮最大时点差 | Kalshi 双边齐全但缺原生时点 |
| --- | ---: | ---: | ---: | ---: |
| 弗拉门戈相邻让球 | 5 | 20 份 | 1.975 秒 | 10 份 |
| 雷默相邻大小 | 3 | 12 份 | 4.505 秒 | 未采此组 |
| 帕尔梅拉斯胜平负三项 | 3 | 18 份 | 1.982 秒 | 9 份 |

两批归档位于私有运行目录，文件权限 0600。该次试采验证了首档深度和时间的留存。样本缺同轮乐鱼真实预检、独立赛况和费用证据。下一批在观察真实预检时同步采集，优先寻找带原生时点的第二条独立行情。同一市场的多个选项只算一个来源。 [订单簿采样器](reference_orderbooks.py) [采样回归](test_reference_orderbooks.py)

## 第二条实时行情

11 月 18 日 23:36 至 23:40 UTC 核对的 Kalshi 官方文档显示，认证后的 `ticker` 行情提供 YES 买卖价及首档数量，`msg.ts_ms` 记录市场更新时点，`sending_ts_ms` 记录网络排队时点。行情会合并中间变动，所以保留为首档快照证据。握手需要 API 凭据，仅看行情可使用市场数据权限。SX Bet 的订单簿快照也要求 API 密钥。生产采集、API 和执行器未发现相关环境配置，尚未读取两家的真实认证行情。 [Kalshi 实时行情](https://docs.kalshi.com/websockets/market-ticker) [Kalshi 认证握手](https://docs.kalshi.com/getting_started/quick_start_websockets) [SX Bet 订单簿](https://docs.sx.bet/api-reference/get-market-orderbook-snapshot)

已验证离线重放入口。经过授权的行情客户端应在收到帧时保存实际 UTC 时间，按行送入 `/research/reference_orderbooks.py --replay-kalshi-tickers --kalshi <market-ticker>`。每行包含 `received_at` 和原始 `frame`。解析器核对市场编号并原样区分更新时点、发送排队时点和客户端接收时点。缺原生时点、未来更新、非正数量和其他市场的帧不会进入有效报价。37 项 Docker 隔离测试通过，流式入口按官方字段构造正反样本验收。 [行情解析](reference_orderbooks.py) [回归测试](test_reference_orderbooks.py)

接入时先用只读行情权限验证订阅、重连和接收时点的连续性。断线或数据间隔过长的区间应留空。再取 Polymarket、第二家独立行情与乐鱼真实预检的时间交集，按完整合同和赛况核对报价及深度。开户、认证、盘口覆盖和真实交易费用还需各自的证据。

## 胜平负盘口与平手盘换算

两家盘口各自提供主胜、平局、客胜的二元市场时，离线定价器可使用 `three_way_books`。该模式支持足球全场常规时间的胜平负和零让球。每家在 `outcome_quotes` 下提供 `home`、`draw`、`away`。三项的参考合同均填写 `kind: winner`，并各自声明所选项。每项保留 YES 和 NO 的买卖价、规模、原生时间与接收时间。仍要求两家不同来源、同一合同、最大时点差合规，滚球还要求两家与乐鱼的比赛状态一致。

先用同一二元盘口的 YES 和 NO 深度确定每项概率区间，再要求三个区间能共同组成一个概率分布。胜平负按所选项最不利的可行概率估价。平手盘把平局归为退回本金，以所选方最少胜份额和同一可行分布下对手最多胜份额计算成本后的最低赔率。

以 23:32 至 23:33 UTC 的三轮 Polymarket 归档复算，主、平、客的区间分别为 0.40–0.41、0.26–0.27、0.33–0.34。在该单一来源和区间假设下，客队平手的成本前保守分界约为 2.2424，有效本金比例下界为 0.73。三轮归档都晚于原赛前成交且缺同步的乐鱼预检。完整定价入口均拒绝单源输入。 [离线定价器](market_reference.py) [结算与边界回归](test_three_way_books.py)

## Polymarket US 公开订单簿

11 月 18 日 23:54:17 至 23:55:37 UTC，用公开接口采集 11 月 19 日 22:30 UTC 开球的瓦斯科达伽马对巴西国际。两边赛事目录的球队、开球时间和正常完赛期间一致。US 的比赛延期规则可能使用最后的公平市场价格，国际站的无结果规则单独规定各选项的结算。这些区别影响对三项概率与异常赛事的解释。 [美国市场赛事](https://gateway.polymarket.us/v1/events/slug/bra-vas-int-2026-11-19) [国际市场赛事](https://gamma-api.polymarket.com/events/slug/bra-vas-int-2026-11-19)

| 五轮固定采样 | 双边报价及来源时点 | 每轮内部最大时差的范围 | 最旧一份报价的年龄范围 |
| --- | ---: | ---: | ---: |
| 国际站，三项各两个选项订单簿 | 30 份 | 0.459–38.229 秒 | 0.625–39.783 秒 |
| 美国站，三项各一个 YES 买卖订单簿 | 15 份 | 109.687–166.497 秒 | 112.824–192.531 秒 |

五轮无请求失败。美国站样本均为开放状态，并返回 `transactTime`、USD 价格及首档数量。采样器保存原始亚微秒精度字符串和本地接收时点，核对市场标识，并保留停盘或缺边状态。一轮客胜相距 2.239 秒，当时国际站买卖价为 0.17–0.19，美国站 0.17–0.18。同期三项仍无法配齐，两地亦缺同轮乐鱼执行预检。公开接口可用于重复取证。美国站的中央订单簿与国际站的链上结算架构有不同的交易机制，仍需评估流动性来源相关性和异步报价对估值的影响。 [美国站订单簿](https://docs.polymarket.us/api-reference/markets/get-market-book) [美国站市场机制](https://docs.polymarket.us/concepts/market-data) [国际站订单生命周期](https://docs.polymarket.com/concepts/order-lifecycle)

三笔自然成交涉及的巴甲赛事也都出现在美国站目录。本次逐场读到的挂牌仅覆盖全场胜平负，无法覆盖雷默的大小球及弗拉门戈的让球组合。对其中的克鲁塞罗对帕尔梅拉斯，滚球时在 23:57:25 至 23:58:45 UTC 又采了五轮。国际站 30 份、美国站 15 份均保有双边深度和原生时点。跨两地完整三项的原生时差为 11.460–28.286 秒，美国站内部三项为 7.441–25.832 秒。23:58:00 UTC 乐鱼投影在第 26 分钟，胜平负报价最近原生更新时间为 23:57:25.196 UTC，投影比分未知。该时段未取得同时刻执行预检和独立赛况，留作报价覆盖证据。 [克鲁塞罗对帕尔梅拉斯](https://gateway.polymarket.us/v1/events/slug/bra-cru-pal-2026-11-18) [雷默对沙佩科人](https://gateway.polymarket.us/v1/events/slug/bra-cre-cha-2026-11-18) [弗拉门戈对巴拉纳竞技](https://gateway.polymarket.us/v1/events/slug/bra-fla-cap-2026-11-18)

## 复跑

历史脚本的 `--kalshi-peer-ticker` 可重复传入一至两次，选择原盘口与其他盘口共同拥有有效买卖收值的最近已闭合分钟。缺共同分钟时返回空值。以下复现第三单的共同分钟。 [历史采样脚本](reference_history.py) [回归测试](test_reference_history.py)

```bash
docker run --rm --network host --cpus .25 --memory 128m --entrypoint python \
  -v "$PWD/reports/profitability-pilot-20261118:/research:ro" ai-football-verifier:v2.0.0-dev \
  /research/reference_history.py 2026-11-18T12:06:10.318000Z \
  bra-fla-cap-2026-11-18-spread-home-1pt5 KXBRASILEIROSPREAD KXBRASILEIROSPREAD-26NOV18FLACAP-FLA2 \
  --kalshi-peer-ticker KXBRASILEIROSPREAD-26NOV18FLACAP-FLA3
```

当前订单簿可用同一挂载，改用 `/research/reference_orderbooks.py`，分别通过 `--polymarket` 和 `--kalshi` 传入已核验的公开市场编号。每轮输出一行 JSON。用 `--samples 5 --interval-seconds 20` 可复跑相同的有限采样窗口。

美国站使用 `--polymarket-us atc-bra-vas-int-2026-11-19-vas` 传入公开市场标识。对应国际站传入 `--polymarket bra-vas-int-2026-11-19-vas`。平局和客胜分别使用赛事目录给出的 `-draw` 与 `-int` 标识。每轮最多查询八个市场。 [美国站市场标识文档](https://docs.polymarket.us/api-reference/markets/get-market-by-slug)
