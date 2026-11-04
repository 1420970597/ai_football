# E216 零候选检索的有界恢复

更新时间：2026-11-04 11:56 UTC。状态：设计已记录，未部署。

目标：缩短原名与平台音译不一致时的无效检索，避免实际存在的比赛被描述为不存在。责任目录 integrations/hermes、tests、docs/platform-v2、reports。前置为当前 Hermes 深度模型、MCP 完整候选检索和独立冻结验证；不增加工具权限。

证据：真实 11:17:54“半大0.5”任务使用图片双方“列宁格勒德／SKA哈巴罗夫斯克”，多次中文、英文、俄文检索未匹配；只读赛事库现有 leyu:5797631，双方“列宁格雷德／伯力能源”，俄罗斯杯资格赛，11:00 UTC。当前 home_aliases/away_aliases 均空。外部别名核验网页工具两次 502，独立公开网页访问也失败，因此没有足够可复核来源把这些名称写入全局等价别名。

设计：只改检索策略，不写入未经核验的别名、不自动绑定。完整原名零候选时，先提取原文中文球队名有辨识度的连续三字片段检索（如“列宁格”），再尝试另一方原名；这是候选召回，不是同队判定。保留足球、原消息日期的合理时间范围；联赛原名与平台命名可能不同，零候选时不继续用未核验的联赛译名作硬过滤，先读候选再核对联赛。完整分页、球队双方/年龄/性别/主客/时间/赛制消歧和当前租约校验保持；能核验唯一身份时使用候选标准名称解析，不能把“只有一个候选”当作对应关系证明。候选仍不符即明确未解决，不再穷举大量无依据译名。

实现动作：独立候选工作树更新中文上下文和专家审核指引；现运行绑定文件在 E215 完整窗口结束前不改。冻结后运行现有 Hermes runner/fixture/传输边界验证，确认固定技能加载、工具白名单和完整候选约束仍生效；自然空闲 gate 后切换上下文及重启 Hermes，正常管理员重审上述真实消息，不写假消息或真实订单，随后重新开始完整三十分钟观察。

验收：真实重审先原名再短片段、返回目标候选且完整分页；如能确认双方、联赛、时间，则仅绑定对应比赛并按当时盘口状态决定是否可录入，不回填过期盘口；不符或歧义必须保持未解决。真实浏览器核对原图、当前模型及系统回执，记录请求耗时和工具轮次。修复后自然新帖逐条有任务、当前attempt模型和回执。失败保留原轨迹，不能为了通过检查强制同名或替换盘口线。

候选冻结验证：独立detached提交 `5cecb3e84ae6cb2f8716c606ff16e18e5bc90287`，仅更新两份Hermes上下文/指引，运行绑定尚未修改。冻结后Docker命令 `python -m unittest tests.test_hermes_runner tests.test_hermes_fixture_contract tests.test_mcp_transport tests.test_review_search_scope` 33项、3.291秒、退出0，证据 `.runtime/e209-live/e216-frozen-targeted.log`。这证明固定技能加载和工具/范围边界未被破坏，不证明模型实际采用新检索策略；真实俄杯重审和新窗口仍待生效后验证。

## 生效与真实样本（12:30 UTC）

运行上下文提交 dd874fa 与冻结候选5cecb3e内容一致，33项隔离验证退出0；在E215完整窗口后通过idle gate切换，仅重启Hermes原容器，健康及实际模型deepseek-v4.1-flash/上下文校验通过，gate/deploy退出0。实际新窗口起点12:20:18.171183 UTC，尚未结束。

真实管理员重审job cbbd648d-1563-4b78-8cc4-06614cf5c45f，原消息5c8bac78-4037-42f4-8c5e-6eca9de3cf8a（11:17:54，正文半大0.5及图片）。首次attempt5e0cda0b已完整搜索原名零结果，再列宁格6候选、哈巴罗5候选，实际工具唯一绑定leyu:5797631。模型读取图片并抽取上半场大0.5；当前上半场已结束、对应报价closed，不生成可投注信号。首次模型205635.548ms、claim-to-conclusion210275.773ms；refresh_unavailable工具回执触发已有一次market_recovery，不能把closed误当唯一重试原因。第二次attempt7fdfeeda刷新written，模型62006.552ms、claim-to-conclusion65876.517ms，最终unresolved，completion_verified=true、同attempt系统receipt存在、工具绑定同比赛、0accepted。不得把此历史补审当成原发布时间成功，更不能回填已关闭价格。

真实Docker Playwright ai-football-browser:v2.0.0-dev命令运行e214-clicks.mjs及e216-browser-samples.json退出0：俄罗斯杯原图/模型/当前系统回执，以及既有已接受原图推荐，各在1440和390视口通过，共4项；页面无JS错误、无弹层横溢，模型文本与API一致、模型/receipt的attempt一致，俄罗斯杯identified_without_signal=1。截图/机器记录在.runtime/e209-live/e216-output/playwright，运行日志e216-browser.log。不把页面可见等同于已生成推荐。

独立E217候选仅改Compose默认CPU，完整当前配置链Docker Compose解析，默认0.25/显式TUNNEL_CPUS=.17两项断言退出0；配置秘密未输出，运行资源尚未改变。实际项目两份env均无TUNNEL_CPUS条目（rg退出1表示无匹配，不是配置失败）。下一步继续E216完整1800秒、自然帖逐条审核与API分项，再决定线上资源调整。
