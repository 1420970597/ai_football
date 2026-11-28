# E233 真实模型引用视觉隔离验收

更新时间：2026-11-04 17:10 UTC。状态：研究及隔离验收准备。

目标：在自然新引用样本缺失时，验证当前deepseek-v4.1-flash通过固定Hermes和生产review_message确实能读引用图片中的球队及盘口、完成工具搜索/唯一绑定/录入。责任目录tests/integration、reports、docs/platform-v2；只使用冻结bef4a43和既有受保护的最小模型提供方配置，密钥只进入独立Hermes，不进入API/MCP或输出。

设计：复用已存在真实模型隔离编排，任务独享临时PG/API/MCP/Hermes，固定镜像及限额；单个合成明确推荐，当前文字确认继续采用引用图片中的全场大2.5，但不提供球队，图片绘制Harbor FC对Mountain United。图片内容来自测试生成，存储读取为显式内存替身；模型提供方真实，工具及数据库真实隔离，生产不写消息/比赛/推荐/订单。断言工具证据完整、唯一两队、全场大2.5、系统1.95，记录真实耗时，不声称自然生产成功或3秒。

失败处理：模型/权限/路由/超时原样失败，单任务截止并清理自身会话及容器，不改生产密钥/模型/框架/数据，不为通过而补伪造工具结果。暂不修改业务实现；测试证据独立记录。

17:12首轮失败保留：第一次修改隔离脚本用错相对工作目录，sed未改模型覆盖，仍沿用旧cline-pass路由，实际HermesProviderUnavailable/3036.726ms；测试finally还误用delete_session签名引发TypeError，acceptance_exit=1。原任务af-hermes-real-e233-467717已由trap清理，未写生产。仅修复隔离编排绝对路径/显式HERMES_MODEL=deepseek-v4.1-flash和真实delete_session(settings,job,attempt)调用后重新冻结快照02运行；不把首轮当模型语义失败或通过，不改生产路由/凭据。

17:14第二轮通过：任务af-hermes-real-e233-473306，固定官方v0.22.5、显式deepseek-v4.1-flash/真实现有提供方，production review_message加载仅引用图1张23537字节；正文未给球队。实际模型通过search/resolve/list_markets/record四类真实隔离工具，唯一匹配Harbor FC对Mountain United、全场over2.5、系统1.95，接受1条，所有工具证据成功，finish=stop/exit0。耗时34003.511ms，within_3s=false；输入是明确标注合成且图片读取为内存替身，不可冒充自然生产样本。前后框架配置哈希一致、技能只读、会话按job+attempt清理，任务容器/网络由trap清理，0生产推荐/订单写入。

实际命令：冻结工作树中bash /root/ai_football/.runtime/e209-live/e233-real-model.sh，日志.runtime/e209-live/e233-real-model-02.log；输出acceptance_exit=0证据目录.runtime/e232-candidate/.runtime/af-hermes-real-e233-473306。当前结论是该真实模型能够理解并通过引用图审核链路，3秒和持续生产正常仍未验收。
