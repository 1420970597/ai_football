# E232 自然图文盘口录入拒绝

更新时间：2026-11-04 16:44 UTC。状态：研究中，未改业务代码。

目标：查明真实推荐已正确识别比赛与盘口却三次被报价校验拒绝的原因，并降低从消息到回执的延迟。责任目录services/api、services/collector、tests、reports、docs/platform-v2；依赖只读原attempt工具快照/刷新证据和当前盘口事实。

证据：E229当前窗口新增8条eligible消息，读取时8job/8receipt无遗漏；其中7条广告或表情图片无推荐，fe833486-d3ce-4f50-bcb4-33baa46c88cb图片指向费内巴切对利物浦、主队全场受让1.25，已唯一定位leyu:5635230与quote b67e56a8-4691-4d19-9a77-b1ebccdd3863，却3次quote_inactive_unknown_or_stale。当前第二attempt模型204819.859ms，event到结论547797.460ms，不能称3秒达标。第一attempt与实际刷新结果待查，不能先把拒绝当错误或关闭新鲜度限制。

研究/设计：有界读取本job全部attempt的输入、失败、盘口查询与录入工具快照，按call_id和SHA复原；核对当前source_updated_at、status、停盘、确认观测与实际刷新错误。若来源确实停用或无法核验，保持不生成可投注信号并展示精确原因；若有效事实在校验链路中丢失，先隔离复现后修正。原文倍率不参与比较；不提高报价有效期、不伪造上游更新时间、不写测试推荐到生产。

首诊断失败：job不存在first_claimed_at列，UndefinedColumnError/exit1，保留stderr；删去错误列后同只读查询重跑，不调整DB结构或放宽截止。等待诊断结果及正式窗口终审；E230已冻结通过的引用图片修复不混入本缺口。

16:54时效设计：02原job快照及confirmation-timing只读命令均exit0，实际max_age=15秒。两attempt共5次录入，耗时13.491/15.776/14.599/3.364/16.136秒；多个查询can_record=true。当前record准备阶段只要旧确认当时有效就跳过refresh，但随后还有第二事务/身份/盘口锁/报价读取/配置读取，最终再校验时可过期。该竞争需可控时钟与真实PG复现，不能仅凭时间近似推断所有5次同根因；其他refresh_timeout/不可用仍需保留。

拟实现：对上游时间已经过旧、依赖单场确认的报价，准备阶段要求确认至少余下5秒（与当前单条DB命令预算相同）才复用，否则使用现有force刷新已读且绑定的fixture。最终仍严格原15秒、原盘口/价格/比分/来源身份验证；不续写source_updated_at、不延长确认有效期、不增加并发或原最多4场刷新预算。全新确认继续避免重复上游I/O；刷新失败若最终旧确认过期继续拒绝。先旧版负向再最小候选及现有fresh/expired/suspended/unbound/historical/多项/价格变更测试，冻结完整回归后才上线，不改E230当前窗口。

17:00隔离复现/实现：真实临时PG、可控业务校验时钟，不做真实sleep，旧确认在准备阶段余3秒、最终写入4秒后，旧实现单项1.731秒exit1/quote_inactive_unknown_or_stale；候选提前刷新后原项及全新确认不重复I/O/已过期重读3项4.807秒exit0。追加刷新503时不接受不产证据、原始报价临近过期的同一边界；后者未扩展保护时1项1.463秒exit1/未刷新，故最终准备阶段对报价和确认都要求5秒余量，最终仍原有效期。对quote_time不改、引用图不变、真实最终证据匹配有断言。

定向02未通过：64项85.890秒有1导入错误，PYTHONPATH遗漏/source导致tests包无法导入，不计通过；已修正为/source:/source/services/api:/source/tests，完整原定向03重跑中。源码在每次运行期间不改；当前未部署，E230正式窗口继续到17:20:30后终审。

17:02定向03通过：90项113.715秒exit0/0失败错误跳过，覆盖reviews、quote_observation、review_tool_evidence；临近过期原报价/确认、刷新失败无信号无证据、年轻确认不重复上游调用、原source时间保持、现价更新、停盘/未知时间/错误盘口/跨fixture/历史/越权/完整分页等原契约通过。候选仅准备刷新策略与3项行为测试，不改最终录入校验。下一步冻结同源完整后端与固定框架，待E230窗终审后再安全部署；自然图文未成功的旧回执保持原样。

17:07冻结框架通过：bef4a4320ba65e0248a36d8f5c27a8b1de36e3d9固定官方v0.22.5完整框架exit0（af-hermes-framework-441638），取消清理239.0ms且其他会话完成/0信号；reply_image/normal各260候选13页及工具接受通过，攻击/长度/并行/无意图/503/重启哈希/预算全通过。提供方与对象读取为显式隔离替身，不证明真实模型3秒。同源API镜像sha256:736c2dff058370ec7dcd4bd10800ef0ebc0ee90c57c1ba98d8af71d8ccaaabfc/revision一致，受限builder构建exit0；API/worker-only覆盖配置check exit0，正式窗口结束和完整后台通过前不部署。独立E232定向PG及网络已清理，不动其他数据。

17:14完整后端通过：同冻结bef4a43，bash infra/test/run.sh exit0，1276项571.293秒/0失败错误跳过，恢复差异{}；机器报告总包装573.765秒、SHAef97d43c55cf2f9782c58e48cb6c7de1178f8aee3425b50be023f4ffa73f96b0，.runtime/e232-candidate/.runtime/test-evidence/441639/backend-tests.json，committed_source_verified=true。候选全检查通过，当前生产仍E230，等待17:20:30之后完整终审再idle部署API/worker，不提前改变观察窗口。
