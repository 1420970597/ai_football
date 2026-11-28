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

## 开球前后的赛况采样

同一场比赛沿用乐鱼赛事、两个公开盘口目录和 ESPN 赛事编号的绑定。赛况采样逐次保存事件编号、主客队、报告比分、阶段、本方请求与接收时间，以及逐事件序列、比分与 `wallclock`。赛前缺字段时保留空值。同秒事件和后续更正保留各自记录。 [ESPN 赛事摘要](https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401910015) [赛况采样入口](reference_basketball_state.py)

02:32:13 UTC 的乐鱼只读样本短暂出现 0 比 0，赛事仍标记为赛前，原生状态时间为空。下一轮比分恢复为空。同期 ESPN 仍为赛前且没有逐事件数据。用阶段、比分和原生时间一起核验，比分单独出现只能作为观察证据。

02:27:50 至 02:33:20 UTC 的 16 轮订单簿中，国际站 96 条选项和美国站 48 条长侧记录都提供了原生双边深度。国际站接收时的报价年龄为 0.014 至 10.621 秒，美国站为 1.954 至 37.641 秒。同一轮两站全部盘口的原生时差为 13.619 至 36.584 秒。同期 16 轮乐鱼投影均为赛前，没有该场真实执行预检。改正后的 ESPN 采样从 02:28:54 UTC 开始，首批 16 轮均为赛前，尚无逐事件数据。

第二批截至 02:40:43 UTC。17 轮外部订单簿全部 153 条选项记录都有双边深度及原生时间，跨站同轮时差 13.070 至 35.957 秒。17 轮乐鱼投影仍为赛前，3 轮短暂显示 0 比 0，全部缺原生状态时间。ESPN 18 轮也保持赛前且无逐事件数据。未观察到该场真实执行预检。计划开球时间后仍应以赛况源可核验的阶段为准。样本只能说明这段时间的报价覆盖和阶段证据，尚不能计算实际开赛延迟或滚球价差。

## 首节对照

02:52:32 UTC 首次读到 ESPN 已开赛。02:53:10 至 02:54:10 UTC，四次并行读取的 ESPN 与乐鱼原生单场比分均一致，依次为 6 比 8、6 比 8、6 比 8、6 比 9。乐鱼各轮缺原生状态时点，ESPN 最新得分事件在读取时已过去 42.42 至 81.86 秒。这个数值也包含比赛在两次得分之间经过的时间。同期四轮外部报价共有 36 条双边记录，跨站同轮原生时差 19.185 至 28.522 秒。02:53:23 UTC 的生产账本查询该场为零推荐、零已验证预检。四轮可交叉核对首节比分。同步可成交价和可追溯的乐鱼状态时点仍待取证。 [ESPN 逐事件摘要](https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=401910015)

同日另两场交叉复核时，雷霆与老鹰比分在两个来源分别是 83 比 83 和 81 比 83。独行侠与勇士两边都为 87 比 60。两组缺乐鱼原生状态时间，无法确认跨源状态的时间先后。报价放行需要在实际预检时逐笔确认两边状态、合约、报价时点与可成交深度。

```bash
docker run --rm --network host --cpus .25 --memory 128m --entrypoint python \
  -v "$PWD/reports/profitability-pilot-20261118:/research:ro" ai-football-verifier:v2.0.0-dev \
  /research/reference_basketball_state.py 401910015 DEN BOS --samples 16 --interval-seconds 22

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
