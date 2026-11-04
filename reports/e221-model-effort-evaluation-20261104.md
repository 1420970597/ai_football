# E221 指定模型推理强度对照

更新时间：2026-11-04 14:10 UTC。状态：研究中，生产配置不变。

目标：在用户指定deepseek-v4.1-flash上评估推理强度能否减少图文意图抽取长尾，保留Hermes/MCP精确绑定边界。责任目录integrations/hermes、services/api、tests、reports、docs/platform-v2。依赖现有受保护网关与可授权原始图文样本；模型提供方密钥仍仅Hermes持有，不输出。

证据：简单单探针low/none约3.4秒、默认约10.7秒，但非随机对照不能据此配置生产。真实单场工具审核145秒、多场超过600秒且低候选量仍超时。已只读固定Hermes源码，gateway/platforms/api_server_request_options.py明确接受model_options.reasoning.enabled/effort和reasoning_effort，不应盲传顶层OpenAI reasoning_effort以为Hermes已生效。项目当前未设置该选项。

动作：准备原8场纯文本和已验证原图样本，在Hermes容器内部直接调用同一实际提供方deepseek-v4.1-flash，默认/low顺序交错、每次有90秒整体截止，结构化比较场数、双方球队、玩法、期间、选项线方向及图片读取，保存耗时/成功/usage和去敏结构化结果，不保存密钥/URL。并发保持1，不向TG发送消息、不调用record_expert_signals、不制造生产审核任务。

局限与决策：此阶段仅意图抽取，不含完整候选检索/MCP往返/排队，不能将其耗时称端到端。若low样本质量不劣且耗时有支持，再设计受版本管理的Hermes配置候选、明确参数传递测试、真实任务同标准比较；未验证前保持生产不变。超时或上游拒绝保留失败，不绕过权限、不降级为猜比赛/盘口，也不承诺所有请求三秒。验收须真实文字和图片、多场完整性及工具绑定均有证据；失败回到原配置，不改原记录或关闭业务校验。

14:16对照初步失败：两轮默认/low文本均JSONDecodeError，第二轮保留响应后确认finish_reason=length、completion_tokens=2000，content包含长篇推理而未结束成JSON；不是模型检索失败，也不能通过截取不完整JSON算成功。图片默认4325.2/3193.6ms、low7611.5/5444.9ms均抽取上半大0.5/两队一致，样本很少且缓存不一致，不支持low更快。none文本同样length/2000token/14192.5ms，图片3415.8ms正确；不能认定reasoning_effort=none在此网关实际关闭推理。仅实验再测显式thinking.type=disabled，不改生产；试图查官方文档web工具502退出，证据不足不宣称提供方支持已核验。首轮失败未存finish/content，第二轮才补齐，保留前轮失败不替换。原图/文本样本0600受限临时文件，仅Hermes私有/tmp使用，输出不含网关URL/密钥。
