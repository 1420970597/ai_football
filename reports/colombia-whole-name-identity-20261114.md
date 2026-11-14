# 哥伦比亚完整队名及秋季查询范围核验

更新时间：2026-11-14 21:03 UTC。目标：修复自然专家图文中的完整队名召回，不补来源期间或数字线、不回放旧推荐。

## 真实缺口与设计

20:57:22 UTC 自然图文明确国民竞技/云斯卡尔达斯、哥伦甲秋，大小1.5但没有期间。原生检索先原联赛零项，再全量同队返回哥伦比亚甲级联赛国民竞技/卡尔达斯十一人，严格双方身份仍零匹配；没有信号。责任目录为 services/api/ 的已核验整名词表与联赛查询范围，前置条件为可审查跨来源身份链。

登记足球、哥伦比亚甲级联赛范围内的完整同队名组。仅精确“哥伦甲/哥伦比亚甲组联赛”规范化；“哥伦甲秋”保留原标签，通过已有查询范围接口召回其已核验母联赛，不伪造上游秋季字段。最终双方、真实联赛、项目、时间及唯一候选继续逐项校验；多个真实同队比赛仍歧义。原图缺期间仍只识别，不读盘/录入。虚拟、女子、青年、预备及乙级不在范围内。

## 可复核来源

- 捷报联盟资料 https://info.nowscore.com/jsData/teamInfo/team250.js?version=2026111505 同一编号2206给出云斯卡尔达斯/卡達斯/Deportiva Once Caldas；同一联盟250给出哥伦比亚甲组联赛/Categoria Primera A/哥伦甲。响应SHA256：2908f2842f4e25f37f0c37a52381f9a5c861fc92a74495cf56649a5e5cc48479。
- 捷报球队原始资料 https://info.nowscore.com/jsData/teamInfo/teamDetail/tdl2206.js?version=2026111505 同一2206队给出上述名称、哥伦比亚Manizales、Estadio Palogrande、1961-1-15及俱乐部官网oncecaldas.com.co。SHA256：b89a4af6bf77ba0ad9003d7c1a7f477eeeb742e3088690744bc1de717718b626。
- 中文百科API action=query/prop=extracts|revisions/titles=卡尔达斯十一人，page2843026/revision86174735：卡尔达斯十一人竞技协会/Corporación Deportiva Once Caldas、哥伦比亚马尼萨莱斯；联合多标题响应SHA256：eec22813345062ef1674cba3dd09ca1ba5b1a8c25739f9e79e1feb9e0874db3a。其他猜测中文页missing，不作为证据。
- 英文百科API page1691553/revision1384383536：Once Caldas、Manizales、Estadio Palogrande、1961、Categoría Primera A，独立交叉核对身份。响应SHA256：25b19bc2826c9b58772943036bd545a2dfb67543c4f9345d7596bc41d28029fe。
- 实际俱乐部官网 https://www.oncecaldas.com.co/ 页面标题及组织结构给出Once Caldas DAF；与球队资料的官网引用一致。响应SHA256：59433725c0e8035a31668772132d68b215f73885ac806fb3ef12e49d3070fbec。
- 捷报2026秋季原始赛程 https://info.nowscore.com/jsData/matchResult/2026/s250_364.js?version=2026111505 母联盟250完整名及哥伦甲简称，子联盟364秋季联赛/Clausura。SHA256：2e4dd3072a91d91de564092547e87ae4efead80a147b71ea1257f50785f5ae05。查询范围仅指向同一母联赛，不把春秋标签改成同一阶段。

错误猜测球队编号1130实际为德国球队，1416是德国地区联赛；错误39赛程及猜测路径返回通用页，均已排除。未将搜索无结果、队名相似或比赛同日作为同队证据。

## 验收与失败处理

新增整名/项目/联赛/梯队拒绝、秋季只召回母联赛、真实与虚拟同对阵隔离、完整原名首查唯一及重复真实对阵歧义测试。隔离验证通过前不发布。失败保留原轨迹；缺身份/期间/价格保持未解决，禁止猜测信号或真实投注验证。
