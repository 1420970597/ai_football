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
