# E385 Hermes 推理强度核验

更新时间2026-11-06 01:55 UTC，状态研究中。目标确认固定Hermes真实custom路由的推理默认值与可用低延迟选项，不更换用户指定deepseek-v4.1-flash。责任integrations/hermes、services/api及tests，依赖固定官方Hermes容器、现有密钥和当前真实路由。

当前证据：Docker读取固定运行源码agent.reasoning_params.unset_reasoning_default及providers.get_provider_profile('custom')实际输出enabled=true/effort=medium；gateway API model_options明确支持reasoning_effort低强度并经当前provider profile编码。未证明上游接受或提速，不将源码支持当真实能力。

设计最多四个交替medium/low的无推荐连接请求，只经Hermes当前路由、每次独立会话和45秒整体截止；不读Telegram、不调用业务MCP、不写生产台账，结束逐会话排空删除。脱敏只留状态、token计数、耗时和当前模型，不留密钥及自由正文；不关闭推理，不修改模型/路由/生产配置。若上游拒绝，保留类别停止更改；如果成功，也只证明该短文本请求兼容与观测耗时，不能推出多轮图文推荐三秒。

后续仅在能力和真实图文语义验证通过后才设计配置增量、针对性/框架/完整冻结测试及受控生效；原比赛分页、唯一绑定、盘口有效、价格与结算规则不变。

01:58 UTC受控四请求actual0，均HTTP200/stop、五字连接回复、原Hermes默认路由，全部会话清理成功。交替medium耗时2073.094/2827.057毫秒，low3016.747/2551.991毫秒，四次prompt_tokens均3390，completion_tokens分别4/25/28/27；样本仅2+2且波动，不能声称low提速。当前不修改生产推理强度，不用无MCP短文本的2秒推导完整多轮图片审核三秒；该实验没有Telegram原文、业务MCP或生产台账写。
