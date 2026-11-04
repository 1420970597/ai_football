# E232 自然图文盘口录入拒绝

更新时间：2026-11-04 16:44 UTC。状态：研究中，未改业务代码。

目标：查明真实推荐已正确识别比赛与盘口却三次被报价校验拒绝的原因，并降低从消息到回执的延迟。责任目录services/api、services/collector、tests、reports、docs/platform-v2；依赖只读原attempt工具快照/刷新证据和当前盘口事实。

证据：E229当前窗口新增8条eligible消息，读取时8job/8receipt无遗漏；其中7条广告或表情图片无推荐，fe833486-d3ce-4f50-bcb4-33baa46c88cb图片指向费内巴切对利物浦、主队全场受让1.25，已唯一定位leyu:5635230与quote b67e56a8-4691-4d19-9a77-b1ebccdd3863，却3次quote_inactive_unknown_or_stale。当前第二attempt模型204819.859ms，event到结论547797.460ms，不能称3秒达标。第一attempt与实际刷新结果待查，不能先把拒绝当错误或关闭新鲜度限制。

研究/设计：有界读取本job全部attempt的输入、失败、盘口查询与录入工具快照，按call_id和SHA复原；核对当前source_updated_at、status、停盘、确认观测与实际刷新错误。若来源确实停用或无法核验，保持不生成可投注信号并展示精确原因；若有效事实在校验链路中丢失，先隔离复现后修正。原文倍率不参与比较；不提高报价有效期、不伪造上游更新时间、不写测试推荐到生产。

首诊断失败：job不存在first_claimed_at列，UndefinedColumnError/exit1，保留stderr；删去错误列后同只读查询重跑，不调整DB结构或放宽截止。等待诊断结果及正式窗口终审；E230已冻结通过的引用图片修复不混入本缺口。
