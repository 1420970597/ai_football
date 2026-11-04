# E219 原生让球方向口径核验

更新时间：2026-11-04 12:39 UTC。状态：研究中，未改线上数据/规则。

目标：统一原生盘口、模型所见选项、页面与专家结算的线值含义，防止客队方向翻转。责任目录services/collector、services/api、web/app、tests、docs/platform-v2、reports。依赖旧协议实际字段及不可变推荐/报价证据，当前E216完整窗口保持冻结。

已核验证据：授权备份leyu.saz SHA69e39d8ab42b358c7dd349b46042384f5beea4da77ded8b9c20a8c8a19bcd59e，raw/360_s.txt真实chpid4的hv=1，ot1/on=+1、ot2/on=-1；chpid19 hv=0/0.5，ot1/on=+0/0.5、ot2/on=-0/0.5。另多条1/1.5、1.5、0.5/1同样。经过Docker只读解码，报告仅保存白名单玩法/盘口/选项标签，不输出请求凭据。对应历史前端raw/711_s.txt解码JS SHA7456f4894f4616316d3c26d5b1f19435eb5e6a87f0c0621b59238754312fbe16，在offset995987 compute_bet_item_options读取r.on作为显示选项；offset999352另将t.hv保存为market_value。这证明hv和所选方on是不同字段，不能把hv无差别当客队所选线。

当前parse_quotes两方向均保存hv（身份键也用hv）；settle函数约定line为所选方线且不再对客队取反。实际12:21多场帖曼城-1对应系统away+1、布鲁日+0.5对应away-0.5：这些可能仍绑定了正确原生option，但系统展示/结算口径与原生不一致。不能仅据DB符号判定真实下注选项错误，也不能直接覆写历史信号或翻转全部其他来源线值。

决策待收敛：优先保存原始报价/信号快照不变，显式区分provider_home与selection口径，并在模型意图筛选、展示、结算边界转换；另一方案是新采集统一所选方，但须版本化标识和兼容历史、避免现有同line唯一键把原生selection_id换成另一侧盘口。未经兼容设计不可只在parser中加负号直接部署。订单仍必须使用原生选项编号并核验同一不可变上下文，不改变用户交易许可。

实现动作：逐一核对当前工具投影/筛选、记录回执、公开报价/专家详情、订单原生预检、结算台账及统计查询的line使用；选择有证据且可恢复的明确口径，先写负向测试复现客队1球/半球与分盘的错算，再完成隔离全套，真实只读对比。验收涵盖主客正负/零、全/半场、split quarters、非原生来源、旧推荐与新推荐、编辑/重试幂等、订单选项不漂移、已结算结果版本链。失败保留原记录，不回填利润或删除待结算项。

兼容决策（12:43 UTC）：保留原始DB line/provider选项编号、既有read context和订单原生预检不变；在确证的leyu数字赛事/handicap/市场4或19边界声明line_basis=provider_home，并新增selection_line用于意图和展示。其他来源、未知市场保持selection口径。结算输入显式携带basis，纯函数仅对provider_home客队取反一次；算法升级football-asian-4-native-line-basis，保存新的calculation/版本，不覆写原始报价和推荐。对外原line保持兼容同时增加口径及选项线字段；前端优先展示selection_line。工具line筛选仍为原生兼容口径，意图未确认该口径时先按玩法/期间/方向读完整组，再用selection_line选确切quote_id，避免把专家所选线误传为hv。

已在独立候选完成共享边界helper和纯结算输入，18项Docker单元通过0.009秒退出0；同一5项新行为用例挂旧结算源码，3项failure退出1，确证旧版客队-1净胜1误算win、跨比分/分盘差异和未知basis未拒绝。当前仅候选模块验证，不是全链完成；工具/页面/台账集成与完整冻结验证尚待完成，生产规则未修改。

定向隔离通过（12:55 UTC）：冻结candidate31328e1，原80项使用任务独立PG和只读源码重跑退出0，62.477秒，无失败/错误/跳过；涵盖新原生客队让1净胜1走盘、原推荐/报价line不变、旧win台账保留且新push台账独立、公开接口selection_line=-1，以及工具快照完整性、过期报价和同attempt回执。前两轮3error/4error分别是快照测试新增元数据期望/PYTHONPATH和显式数据库测试地址遗漏；第三轮仍4error实际是worker保护只允许ai-football-v2-pgverify/pgtest主机名，添加任务独立网络内alias并改测试DSN后才通过，未放宽保护或访问生产。源码冻结期间未编辑被测文件。

将已验证API/MCP/web/tests复制到任务分支准备独立全套提交，当前Docker运行镜像不变。两份Hermes运行绑定上下文暂不复制，避免窗口中热变；E219口径说明及E218日期搜索将在各自冻结33项验证后，于完整窗口结束的idle部署一并应用。后续API/MCP/web镜像与冻结代码SHA对应，Hermes上下文另记实际提交、差异和配置版本，不把旧运行上下文称已更新。
