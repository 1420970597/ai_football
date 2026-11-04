# E234 真实审核耗时分解

更新时间：2026-11-04 17:50 UTC。状态：研究中，生产配置和代码未修改。

目标：降低真实文字/图片审核总延迟，同时保留完整候选、精确盘口、消息版本、原价和最终有效性。责任目录integrations/hermes、services/api、services/hermes-mcp、tests、reports、docs/platform-v2；前置为E232已验证版本、真实任务只读trace以及固定Hermes源码。此阶段不增加模型并发，不压缩必要搜索分页，不猜盘口。

当前证据：docker exec -i ai-football-v2-api-1 python - < .runtime/e209-live/e234-review-latency.py，exit0/stderr空；只读3个已完成真实任务，各最多150trace，不输出凭据。dd01e81f模型97.600秒、provider90.896秒、准备图片1.116秒，累计usage prompt123323/completion4692；dbab7c84模型296.634秒、provider290.866秒、准备0.772秒，prompt78241/completion17174；自然编辑50aedf2c模型92.809秒、provider86.088秒、准备0.679秒，prompt124279/completion3172。usage是框架多轮累计，不能冒称单轮上下文或全部推理token；回复任务首次工具trace约在request后198秒，但trace created_at是事务开始时间，不等同精确HTTP结束，不能直接称模型纯计算198秒。

工具实际elapsed：搜索1.863–4.550秒、resolve2.924–6.688秒、盘口10.998–12.924秒、record12.701–13.304秒。许多远端事务往返与模型回合均有成本；仅线程化图片准备不能消除主要延迟。现有API、模型客户端、对象读取已使用async，禁止把异步改同步线程池当成验收。

缺口与设计决定：固定v0.22.5源码明确API可读取request reasoning配置，transport按provider能力映射；当前模型网关是否接受低推理、对文图准确性影响尚未验证，不直接更改生产配置。先追踪实际请求/有效配置与provider映射，确认可配置支持后，仅隔离候选做同图同文字成对真实请求，分别保存模型输出、工具完成、拒绝行为、usage与总耗时；有回归风险或未证明更快则不发布。另沿E231定位DB准备/执行等待，与CPU/TCP及本机无DB健康请求对齐后再选择可复现最小改动。

验收：不能用替身耗时或HTTP200声称3秒；必须真实文图正确绑定、完整工具回执、现有安全边界和全回归通过，生产完整窗口无丢失与异常。失败保留原任务与日志；当前两个约93/98秒正确样本及一个歧义回复约297秒均不满足3秒。不终止会话，继续实际监控与定位。

17:53复核既有研究：E221已实际比较默认/low/none/thinking disabled，单次与图片结果不支持低推理更快，部分文本length由2000输出预算造成；不重复同一探针或据配置项存在就上线。固定源码custom provider非OpenRouter默认不发送reasoning extra_body，providers.<name>.extra_body有正式透传，但网关具体效果仍未核验，保留原配置。E224已证实盘口筛选能减少体积且保留录入项，提示策略受真实治理provenance约束，本阶段不改哈希或伪造人工标签。下一步优先验证真实active_job读查询的服务端执行计划：仅捕获本地函数生成的SQL，将FOR SHARE去掉，在只读连接EXPLAIN ANALYZE有限一次；输出只保留节点、耗时、行数和索引名，不记录SQL/参数/数据。若服务端本身慢再决定索引/查询改动，不能预判为网络。

17:54实际读计划：docker exec -i ai-football-v2-api-1 python - < .runtime/e209-live/e234-active-job-plan.py，exit0/stderr空，实际生产相同active_job查询去FOR SHARE、默认只读/4秒statement/5秒command。服务端Planning5.706ms/Execution0.610ms，客户端472.171ms；主要按主键/消息复合/trace job+sequence索引，未出现全表扫描。此次reply_media_changed分支未执行且无行锁，不能代表所有任务、锁等待或高峰；不足以增加索引。明确至少一个真实常用查询服务端执行很快，远端协议往返/准备/排队仍待分解，不把472ms全部说成网络RTT。
