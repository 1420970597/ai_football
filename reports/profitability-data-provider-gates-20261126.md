# 外部参考数据源的适配边界（2026-11-26）

## 当前结论

截至 2026-11-26 03:17 UTC，登记纸面样本已观察 1,244 条建议、8 条完整原生预检。8 条均为足球滚球大小球，半场与全场各 4 条。8 条独立参考评估均因 `reference_contract_missing` 被拒，现有 14 场参考清单与这 8 场的重合为 0。另有 4 条预检因 `account_stake_outside_available_limits` 失败。参考合同批准和费用证据均为 0。

这表明当前首先缺的是所覆盖赛事、滚球期间和盘口线的精确对照，以及当下能重建的成交深度和账户成本。单纯增加预测模型并不能补足这些输入。

## 候选来源

| 来源 | 可验证能力 | 与当前门槛的匹配 | 未验证项 |
| --- | --- | --- | --- |
| Football-Data.co.uk | 历史结果和开盘后/收盘赔率 CSV，部分近期赛程赔率按周更新 | 适用于赛后校准与长期收盘基准，不提供当前滚球报价深度 | 与 8 条滚球大小球的盘口线、期间及滚球比分不等价 |
| The Odds API | 官方文档列出 live/upcoming odds 和 `totals` 市场。普通赛前/滚球固定赔率市场更新间隔分别为 60/40 秒，交易所市场为 20/10 秒。标准 `totals` 主要出现在美国体育和部分博彩公司 | 交易所赔率标称 10 秒刷新有机会满足 15 秒年龄门槛。固定赔率滚球 totals 的 40 秒间隔不满足 15 秒门槛。历史赔率快照为 5 分钟，不能复原 15 秒历史成交状态 | 当前 8 场赛事、期间、盘口线是否覆盖，接口时间戳到本系统落库延迟、同合同双来源、深度、实际账户手续费均未验证 |
| Betfair Exchange API | 官方接口支持低延迟市场流及赔率/成交量查询 | 可作为精确交易所市场的数据候选 | 当前本地没有 8 场赛事精确映射、可用账户权限、费用与资金适用性证明 |
| Polymarket CLOB | 官方 API 提供当前 YES/NO 二元合约 orderbook 和 bid/ask | 已知的 API 形态是二元事件合约。足球滚球总进球线需要额外映射到唯一可结算命题；滚球开始后体育市场挂单按官方规则自动取消，现有文档未证明覆盖当前这些盘口 | 当前市场映射、同期间/线/比分规则、可成交深度、费用与地区可用性均未验证 |

官方资料：

- Football-Data 说明历史数据和closing odds用途，fixtures odds通常在周五或周二采样。[Football-Data fixture schedule](https://football-data.co.uk/matches/resources/contact.php) · [数据集说明](https://football-data.co.uk/data)
- The Odds API 当前价格页列出免费每月 500 credits，付费 20,000 credits 每月 30 美元；其赔率文档说明 markets/regions 消耗 quota，部分 market 增量收费。当前 `totals` 主流覆盖限制和刷新间隔也见其官方文档。[当前套餐与覆盖](https://the-odds-api.com/) · [接口说明](https://the-odds-api.com/liveapi/guides/v4/) · [刷新间隔](https://the-odds-api.com/sports-odds-data/update-intervals.html)
- The Odds API 允许将数据用于商业客户应用，但禁止作为原始数据流单独转售；具体适用需要依其现行条款及业务范围确认。[服务条款](https://the-odds-api.com/terms-and-conditions.html)
- Betfair Developer Program 描述 Betting、Accounts、Stream API，可查询价格、成交量、余额及市场流。[Exchange API overview](https://developer.betfair.com/exchange-api.jar)
- Polymarket 文档把每个市场定义为 YES/NO outcome token。UI显示的价格可能是盘口中点，交易须用实际 bid/ask；其体育市场挂单在比赛开始时自动撤销。[订单簿与交易价格](https://docs.polymarket.com/concepts/prices-orderbook) · [体育市场规则](https://docs.polymarket.com/concepts/markets-events)

## 确定性与不确定性

- **确定性**：当前本地 8 个原生预检没有任何一个被 14 场参考清单覆盖。8 个定价拒绝码都是 `reference_contract_missing`。目前没有经过批准的费用凭证。
- **确定性**：Football-Data 最近fixture赔率按周采集，不应冒充滚球时点价。The Odds API文档列出的固定赔率滚球 totals 40 秒刷新慢于项目 15 秒参考年龄门槛。其历史 snapshots 以 5 分钟为间隔，不足以重建 15 秒边界。
- **不确定**：The Odds API交易所 market在所需赛事是否提供live totals及真实端到端到达时间未知。Betfair Stream API所需地区账户、赛事市场映射和交易成本未测试。Polymarket是否针对这些具体赛事提供相同足球总进球命题及开球前可用成交量未知。
- **不确定**：上述来源即使能提供合法有效价格，也不能单独产生预测优势。数据接入改善覆盖，仍需独立前瞻样本验证扣除手续费、汇兑、流动性和滑点后的边际。

## 决策与后续验证

本阶段没有购买 API、创建新账户或调用受限赔率接口。未把 API套餐价格转化为预期收益或投资建议。

只读复核运行中的 API 与 reference-price 容器环境变量名称及挂载类型后，没有发现 The Odds API、Betfair、Polymarket 的接入密钥变量。参考服务只挂载本地 reference data、既有 public orderbook/score archives 和价格服务 socket。此检查只确认当前这两个容器配置，不能代替其他备份或外部用户账号盘点；没有读取或输出密钥值。

若继续评估 The Odds API，先仅针对当前支持的精确足球联赛、滚球大小球和 half/full period做只读覆盖检查，必须保存请求区域、bookmaker、source timestamp、接收时间、盘口线和完整价格组。只要连续采样无法满足原 15 秒门槛，就不进入成本后策略收益测试。5分钟 historical odds适合模型校准，不作为回填成交价。

Betfair 应先只读核验原账户是否可取得交易所 API stream、目标市场 catalogue/orderbook和 commission evidence。Polymarket只纳入能够证明 YES/NO问句、即时比分基础、结算规则、同一盘口线和可成交盘口深度完全相等的赛事。否则继续留作未映射，不降级成近似合同。

最终止损结论仍是：缺少精确独立合同价格与经核验费用时，候选收益必须保持不可估。现有止损回放只证明历史受理队列的损失会随阻断力度而下降，不能证明有任何可持续的正边际。
