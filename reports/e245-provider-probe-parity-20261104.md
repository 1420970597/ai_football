# E245 诊断探针与真实模型 SDK 的差异

2026-11-04 21:32 UTC。结论：旧 urllib 探针的502不能代表当前生产审核不可用；实际框架和 SDK 均有成功生成证据。没有修改模型路由、密钥或绕过模型工具权限。

旧诊断同deepseek-v4.1-flash路由生成返回502，严格错误类别Upstream access forbidden；模型目录200且包含该模型。已用Hermes解析函数核验地址/密钥与探测一致，仅记录布尔，不输出秘密。该现象曾被过早解释为整体上游权限问题，已向用户纠正。

矛盾证据是21:22–21:26真实13条自然消息均有当前模型结果及系统回执。它们没有足球推荐意图，未误生成推荐。不能将其成功与旧探针502择一丢弃。

先以HTTPX且trust_env=False对照stream/nonstream、默认及medium，四组200；再交错max_tokens32/不带/max_tokens128/不带/max_completion_tokens32，五组200、2.28–2.90秒。因此不能认定token参数导致拒绝，也不能仅凭前后时间推断网关已修复。

客户端同条件对照：urllib代理配置为空；HTTPX默认200/2341ms，仅将User-Agent改成Python-urllib/3.12得到502/35ms；Connection: close仍200/3778ms、表单Content-Type仍200/2203ms；urllib默认和明确无代理均502/约27–38ms。该受控差异指向请求标识处理，而非已证实的凭据或模型缺失。未读取或修改网关自身配置，不能进一步断言是何处的策略。

最终使用Hermes容器安装的原OpenAI SDK、不定制User-Agent、同地址/密钥/model、关闭诊断重试，真实200/3274.54ms、stop、4字响应。旧两轮urllib长期探针各只终止唯一识别的诊断进程，exit143记录，业务框架和任务未停止。新探针复用实际SDK，每5分钟最多一次/7次，输出状态、耗时、finish_reason和字符数；不记录密钥或响应正文。

该轻量测试不代表图片理解、多场匹配、工具分页、录入及结论持久化的端到端三秒通过。实际13条新消息框架调用8–49秒以上，队列与结论20–148秒，仍须优化。继续完整窗口与自然消息检查。

主要证据：`.runtime/e209-live/e245-wire-probe.jsonl`、`e245-token-probe.jsonl`、`e245-client-parity.jsonl`、`e245-sdk-parity.jsonl`、`e245-sdk-watch.jsonl`及`e241-current-versions-mid-06.json`。探测全部在Hermes容器中，生产数据库只读审计，没有写入测试消息或投注。
