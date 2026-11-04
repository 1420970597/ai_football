# E230 引用消息图片上下文

更新时间：2026-11-04 16:09 UTC。状态：研究中，未改实现。

阶段更新：2026-11-04 16:33 UTC，f409a05代码已写、定向隔离验证通过；固定v0.22.5框架完整验收exit0（af-hermes-framework-346214）。reply_image使用生产review_message/native结构化视觉路径，提供方收到1张引用图、17次请求、260候选连续13页完整搜索、1条工具接受记录；timeout_cleanup及其他攻击/分页/并行/503/重启/预算场景全部通过。对象读取和模型提供方是明确隔离替身，不能证明线上真实语义或3秒延迟。完整后端回归仍在运行，未部署。

命令与发布准备：冻结工作树运行bash tests/integration/run_hermes_acceptance.sh，acceptance_exit=0；同源services/api/Dockerfile经受限ai-football-e131 builder构建exit0，镜像ai-football-api:2.0.0-dev-e230-f409a05。仅API/worker发布覆盖配置检查exit0，必须等待E229完整16:46:39以后终审、完整回归通过并持有自然idle门禁才可更新。当前正式窗口不改实现或参数。

16:36完整后端通过：冻结f409a05a4517e283280b8421e2fcf73e40b3e991，bash infra/test/run.sh exit0，1273项573.219秒/0失败错误跳过，恢复差异{}；机器报告包含套件包装总时长575.615秒，SHA20610166172cdf2812ca2da260ba7ddbcc4211948b284fae197997502a450047，路径.runtime/e230-candidate/.runtime/test-evidence/346213/backend-tests.json。API镜像sha256:ab8dcf515e1583ac10f7c7bed5aea88e3681276dc87fb71caa5f67beb4f4af55、完整源码revision相符。已结束的独立E230定向PG容器及网络清理exit0；完整套件自身清理，不触及其他服务。状态为隔离验证通过，真实新引用视觉语义尚未验收，未部署。

16:51已安全生效：E229完整终审及原台账before保留后，16:49:25.460255暂停新claim、16:49:28.937444自然idle持有门禁，仅API/worker替换为上述同源镜像，deploy exit0/API ready200/worker running/门禁正常释放。Hermes/MCP/web/tunnel/remote-link容器及资源未变；正式15场rebuild与38信号80台账before/after核验、双端真实浏览器正在进行。已启动新独立1800秒窗口，真实引用视觉语义仍须自然样本，不称专家全部恢复。

16:54发布后验证：15场正常rebuild HTTP200，Docker核验38不可变信号与原价/38结果/80台账及时间全一致exit0；Chromium1440/390各2原图详情exit0，图片解码、同attempt回执、正文/API一致、无JS错误或横溢。新窗精确16:50:29.255802起1800秒，17:20:30后完整终审；不因前段连接正常或回归通过提前结束。

目标：让可信专家新消息引用同群已采集原文时，把原图作为有来源标识的不可信视觉上下文交给Hermes，减少把简称误当另一球队；不能将被引用旧推荐自动当作本条新意图。责任目录services/api、tests、reports、docs/platform-v2；依赖远端已有私有媒体/稳定source及message关联、当前版本/删除检查。

证据：E227自然9f6af4c1图片成功识别哈尔迪亚+1，但随后282eb80f“追對家”虽引用它，仅保存reply_context.text/has_media；worker只查reply_text，hermes_client只准备当前media_objects。因此模型看不到旧图并按Hull搜索0候选。该回复缺明确新盘口，即使补图也不能允许猜盘口/倍率或跳过绑定。原始回执不可改写成成功。

设计待细化：只从相同来源、非删除、远端媒体已就绪的被引用版本解析；相册单成员引用须通过既有event_members索引找父事件，不跨群按相同消息编号取图。保持被引用作者/原消息编号/时间/内容及媒体哈希，可追溯该attempt实际输入；图片结构化传入并明确当前图片和引用图片边界。引用原图同样校验类型/哈希/字节，所有图片共同受10张/24MiB/读取并发2限制，不增加权限。已知引用媒体未就绪必须显式延期/重试，不能当无图成功；没有本地档案则保留不可用原因，不访问任意链接。编辑/删除竞态须可见且旧attempt不能继续接受推荐。

验收：同群原图/相册成员、跨群同编号拒绝、已删除/非当前/媒体pending、完整预算和取消、结构化图序/来源/摘要、原输入证据及最终工具绑定均需测试；冻结后完整Docker和固定框架视觉回路，再真实新窗口自然样本。暂不混入E229候选，优先完成其当前全套/发布前联调。

16:17设计细化及真实档案核实：只读查询282eb80f.reply_to=20318，同source最新原文确为9f6af4c1、media ready/1图/非删除。因此不需要重新抓取上游。worker claim通过同source的直接ID或event_members（相册成员）横向查询最新版本，提取reply archive id/revision/posted_at/author/media_state/media_objects，删除最新版本不能回落旧图；无媒体就绪证据不能传图。已就绪引用图与当前图共同限定10张/24MiB/读取并发2；输入按当前图组、引用图组明确标注，原文“追对家”不推定盘口。known pending/failed引用媒体抛明确可重试reply_media_pending，回执不会成为无图成功；unsupported/未采集仅显示不可用原因，不跨群或任意URL抓取。输入追踪保存该attempt引用档案与图哈希；不改当前原图归属或原推荐证据。固定E229窗口运行中只改未挂载API源码，候选冻结后定向/全套再上线。

16:23定向证据：已有客户端15项8.310秒exit0；补共享图片预算/当前与引用结构化分组/待上传明确可重试/已删除或unsupported不读取，17项8.382秒exit0。真实PG同source直接与相册成员、回复快照兼容/跨群隔离/输入trace四项6.327秒exit0；引用图开始后编辑/删除使active_job工具权限409单项1.335秒exit0。扩展worker/reviews/图像/客户端全定向进行中，运行期间源码不改。引用归档检索使用既有event_members bigint索引，正则校验安全数字后转换查询参数，未对高基数列转text全扫。输入trace保存reply_archive精确版本及媒体列表；active_job同一SQL核对非删除/同source/版本和图列表/无更新版本，已失效引用不能产生有效工具调用。未部署。

16:26原缺口负向及定向通过：旧264770f同图片用例只读取current.png而漏reply.png，1项0.005秒1failure exit1；旧worker无reply_archive，真实PG1项2.871秒1error exit1。候选完整定向134项200.181秒exit0，无失败错误跳过；包含现有多专家调度、重试、录入边界、continuation、图片并发/字节/类型、精确候选等。源码冻结后不在测试中修改；定向结束后新增固定框架reply_image场景，通过生产review_message准备引用结构化图片，仅对象读取为显式隔离替身，要求提供方实际收到匹配哈希、完整260候选13页及工具录入。此提供方仍为确定性替身，不评价真实语义。待冻结完整后端和全框架验收，不上线未验证候选。
