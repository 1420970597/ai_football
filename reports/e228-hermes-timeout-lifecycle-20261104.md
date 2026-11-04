# E228 Hermes 超时生命周期调查

更新时间：2026-11-04 15:28 UTC。状态：研究中，未修改生产。

目标：确认600秒审核超时/任务租约结束后，框架是否停止该attempt的模型工作，避免过期工作争用CPU和模型并发。责任目录services/api、tests、reports、docs/platform-v2；依赖固定Hermes v0.22.5源码和真实只读状态。现有worker会在finally删除expert-review专用session，但框架删除接口对active_write_guard返回409，删除本身不执行interrupt。日志出现3次session cleanup HTTPStatusError，尚未证明都是409或残留任务。

15:24左右只读health/detailed返回active_runs=0、stored_runs=0、valid running review jobs=0，metrics为零；此快照不能证明过去不存在残留，也不能把计数为零解释成历史调用成功。框架SSE断开路径存在interrupt/reap处理；普通非流式调用取消是否覆盖线程执行仍须逐层核对。不得根据上述风险直接重启、取消其他有效任务或修改框架全局源码。

设计与验收待核对后记录：候选如采用SSE，必须保留整体截止、图片结构、幂等与session、最终finish_reason、usage及不完整响应失败语义，并实测断开取消；不能用部分输出代替已接受工具回执。先做有边界生命周期复现，再决定实现。现阶段优先完成已冻结E227完整回归和合并部署，E228不混入候选。

15:44源码复核修正：实际开启session_authority，_run_agent在旧adapter计数之前委托gateway/session_api_turn.py；其中asyncio.shield(waiter)明确让canonical turn存活于请求取消之后。因此此前api_server.active_runs=0不是完整活动数，不能用来排除残留。health.active_agents另见2，尚不能把其与读取时一个valid DB job的差额直接归因。一次诊断误用role=review-worker退出1，改worker后GET200并确认项目Hermes/default路径，无secret输出。只读核对3个先前timeout attempt：精确派生的chat request run ID均GET200、session逐一匹配、当前completed；这证明控制定位契约可用，不证明各自在旧超时之后才完成。

设计已记录：保留非流式与现有幂等、600秒整体预算。仅DELETE session返回明确409 session_active_turn时，在原总清理截止内用固定框架default profile+API key散列派生attempt及两次continuation的原run_id，GET认证投影，只有session完全等于本job/attempt且queued/running等活动状态才POST该run/stop，终态/404不停止；再DELETE验证。身份不符、403、未知409/状态必须失败可见，不猜编号、不停止其他任务、不修改已接受推荐。框架官方session_authority stop具备queued取消和generation匹配中断，优于盲改SSE。带profile URL须按框架已核验profile作用域，否则拒绝派生。先客户端合约/越权/预算及真实固定框架隔离阻塞调用→取消→清理→精确run终态/其他会话存活；再冻结定向全套。不得手动调用生产stop来替代隔离验收。

15:49客户端实现及定向：非流式请求不改，仅409 active按认证scope/已知3个幂等key查询和校验session后停止；8秒整体清理截止，禁止跨session或unknown状态控制。客户端7项8.227秒exit0，包括正常/无关冲突/403/终态404/其他session/未知状态/阻塞取消上限。旧实现单例负向返回HTTP409错误，保留e228-negative.log。新增固定框架隔离场景：两个独立会话各阻塞提供方5秒，取消一个HTTP observer后先证明DELETE409，再调用新cleanup，验证另一个会话正常完成且没有推荐写入。尚未跑框架，不计真实联调通过；无生产stop操作。
