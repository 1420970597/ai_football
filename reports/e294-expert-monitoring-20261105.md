# E294 API前置事务优化后自然监控

更新时间：2026-11-05 08:02 UTC。状态：监控中，尚未终审。

目标/责任：E292 API单服务优化后，持续六群Telegram文图持久化、Hermes工具审核、真实结算和网页原图/状态验收，涉及services/api/、services/collector/、integrations/hermes/、web/app/及reports/。前置为旧网关deepseek-v4.1-flash、原TG授权、远端事实与媒体，不提交测试发言或资金订单。

窗口开始07:33:28.254903 UTC，独立容器ai-football-e294-independent-watch，每30秒有界采样，最早08:03:28完整截止。API保持E292 f66aa55b；期间仅web经独立E296验证后更新，原浏览器加载故障及该变更单列，不能把软件变化隐去。首样本TGstate unavailable但连接授权true、degraded false、六有效租约；后续持续采样以实际终审为准，不提前称整窗健康。

## 已核验自然样本

65428100-1e38-4ecc-bd07-9756da14b539：纯图片、谢珀顿U23—库巴厄XI U23，76:41/0:7，选中全场小8.5；原图赔率1.79，系统保存1.64。原图已授权获取并目视核对。精确匹配/工具接受1项，端到端90601.829ms、模型75973.033ms，三秒不通过。工具两次队名检索3292.7/3928.383ms、唯一消歧2856.24ms、读盘9315.797ms、提交12131.619ms；不能用不同自然样本作严格优化对照，也不能把整个Hermes请求耗时当纯模型推理。

同场官方赛果已confirmed版本1，全场0:8/半场0:4。新signal 909d603b-29fe-4b25-b74c-20a5b1c055e8实际win/unit_net+0.640000，理由asian_line:score_basis_outcome_equivalent。原55d0d577上半大3.75虽比赛当前finished，但全段计分与剩余计分产生不同结果，实际pending/live_market_score_rule_unknown，未假结算。其他两原推荐继续独立2小时跟踪。

9f90a763-de89-41be-864a-d38c746cc53a：原图德拉萨尔绿弓箭手—UE红战士，下半79:21/8:0，选中全场小9@1.85；原线closed、现8.75为另一线，未替换且0接受，模型122241.751ms/端到端141423.193ms。原图已目视；拒绝原因有工具证据，不是队名未命中。

c477eb03-14a5-483e-98f2-72af6f9a643f：原图相册为火鹰FC—铁虎FC上半01:08/0:0旧小2/2.5及较晚中场1:1，正文收米；两张原图均已目视，正确反馈0新增推荐。第一版浏览器只等当前已出现第一张图片就停止收集响应，后续完整相册导致测试assert失败exit1；改为按消息实际media_objects数量等待全部图加载，原失败保留，未改产品或放宽图片验收。

## 实际浏览器及保护

- e294-album-images-clicks.mjs：Docker内1440/390共6场景actual exit0，1接受/1闭盘拒绝/1反馈，同attempt系统和模型回执、当前阶段、原图授权读取及无横向溢出；保留四张授权原图在受保护.runtime，不提交私有媒体。
- e294-settlement-clicks.mjs：同一成功会话复用，双端两个真实消息共4场景actual exit0；当前比赛finished与推荐时live分别显示，win及pending与API一致，原图/系统收益入口正确。无登录限流变更、配置修改或资金订单。
- E296发布后五phase及专家/会员/个人入口双端共16场景通过，e296-phase-artifacts-browser.exit0，截图/报告在e296-phase-evidence/output/playwright/。首次同样用例exit0但产物卷误挂/work，未保留截图；纠正/evidence后重跑产物完整，未用首轮截图不存在宣称证据齐全。

最终窗口计数、源异常、API/桥错误及剩余任务将在实际截止后追加。若有任务未完成继续追踪，不能以HTTP200或容器exit0替代业务验收。E295远端预加载/预检查0、16文件与固定镜像一致，窗口结束前不切collector；E297冻结回归仍进行。

08:07 UTC 最终窗口：07:33:28.254903—08:03:30.791809，56样本1802.5秒，watch/finalizer actual exit0，SHA1954a30d057d9b426c1809335b5f4e46e9568837ea8f292adb34ce5d06593762。27新帖23ready/4unsupported、189旧帖修订、217创建版本；23当前eligible全部同attempt回执、0缺失/在途。两条当前推荐各接受1项；另c0c75619町田泽维亚主让1.5@系统1.82，端到端84973.917ms/模型73002.931ms，须继续原图页面核验，不能仅模型文字验收。健康端点全200、无坏租约/退避采样，首TG unavailable状态仍保留，整体telegram_all_healthy=false。

API692条events全部200，但07:45:00.524560892赛事批量入库出现一次TimeoutError（请求8ddb1ea7-3729-4c98-9163-177d0653dad8，ingest_fixture_batch:120），诊断明确execution_wait：5000ms预算、prepare0.012ms、execute5003.107ms，不是准备耗尽。远端桥3096条慢/失败记录中fixtures两次503：一次BrokenPipeError，下游回复3398.4ms；一次与上述API超时相关7357.2ms。不能宣称数据库错误0或网络根因修复，需继续有界锁/等待证据研究。
# 补验记录（2026-11-05 08:23 UTC）

四条unsupported的实际manifest均为无正文video/mp4，不将它们计为图片理解遗漏。先前未核验的町田泽维亚-1.5自然图文已完成1440/390真实浏览器两场景exit0、原图目视及同attempt/accepted1/当前阶段核对，详细证据见E298报告。原整窗SQL超时及三秒失败保持未通过。
