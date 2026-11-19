# NBA 赛前外部报价覆盖

2026 年 11 月 19 日 01:45:19 至 01:45:59 UTC，赛前试采凯尔特人对掘金。乐鱼和两家公开目录都列出同场对阵，开球时间为 02:30 UTC。采样覆盖胜负、凯尔特人让 2.5 分和全场 220.5 分。美国站返回长侧订单簿，采样器用同一市场的 `marketSides.long` 绑定选项。胜负、让分和大小分的长侧分别为凯尔特人、−2.5 和大分。国际站直接按两条原生选项记录订单簿。 [国际站赛事](https://gamma-api.polymarket.com/events/slug/nba-bos-den-2026-11-18) [美国站赛事](https://gateway.polymarket.us/v1/events/slug/nba-bos-den-2026-11-18) [美国站市场定义](https://docs.polymarket.us/api-reference/markets/get-market-by-slug)

| 采样 UTC | 国际站同轮时差（秒） | 美国站同轮时差（秒） | 跨站全组合时差（秒） |
| --- | ---: | ---: | ---: |
| 01:45:19 | 2.440 | 8.995 | 24.423 |
| 01:45:39 | 9.449 | 9.626 | 12.719 |
| 01:45:59 | 7.493 | 21.466 | 21.466 |

18 份市场快照均有原生时间和双边首档量。国际站每份市场快照有两条选项记录，美国站每份有一条长侧记录。美国站胜负的 `transactTime` 连续两轮保持在 01:45:33.244 UTC，归档仍保存各轮各自的接收时点。市场最小成交档、费用及真实成交能力需另行核验。 [美国站订单簿结构](https://docs.polymarket.us/api-reference/markets/get-market-book)

两站胜负规则以及美国站的让分和大小分规则均明确计加时。国际站两个附加市场的本场描述未明确加时条款。乐鱼当前只在四节、上下半场和全场分数互相吻合且无加时记录时确认该场计分证据。赛前无法由该逻辑证明同一张合约的加时口径。本次也缺同步乐鱼执行预检、实际费用和两站报价的经济独立性证据。三轮记录用于验证覆盖与来源延迟。 [篮球比分证据](../../services/basketball_scores.py) [国际站赛事合同描述](https://gamma-api.polymarket.com/events/slug/nba-bos-den-2026-11-18) [美国站赛事合同描述](https://gateway.polymarket.us/v1/events/slug/nba-bos-den-2026-11-18)

后续先用已发生加时、且有原生结算的比赛分别核验乐鱼胜负、让分和大小分。覆盖照此扩展到其他比赛和可核验盘口。每笔候选配对同合同的可用外部深度、原生时点与真实执行预检。历史提前量与费用由归档计算，在后续独立样本上复用固定口径。 [采样器](reference_orderbooks.py) [采样测试](test_reference_orderbooks.py)

```bash
docker run --rm --network host --cpus .25 --memory 128m --entrypoint python \
  -v "$PWD/reports/profitability-pilot-20261118:/research:ro" ai-football-verifier:v2.0.0-dev \
  /research/reference_orderbooks.py --samples 3 --interval-seconds 20 \
  --polymarket nba-bos-den-2026-11-18 \
  --polymarket nba-bos-den-2026-11-18-spread-away-2pt5 \
  --polymarket nba-bos-den-2026-11-18-total-220pt5 \
  --polymarket-us aec-nba-bos-den-2026-11-18 \
  --polymarket-us asc-nba-bos-den-2026-11-18-neg-2pt5 \
  --polymarket-us tsc-nba-bos-den-2026-11-18-220pt5
```
