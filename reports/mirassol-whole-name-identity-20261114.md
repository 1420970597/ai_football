# 米拉索完整队名身份核验

更新时间：2026-11-14 21:52 UTC。目标：修复真实巴西甲图文的完整异译名身份召回；责任目录 services/api、tests、reports、docs/platform-v2。

## 当前证据、缺口和决定

21:41:51 UTC 自然消息明确巴西甲米拉索/米内罗竞技，原生候选为迈拉索尔SP/米内罗竞技。完整双方校验未绑定，未生成信号。图片仅比分及“破蛋”，没有所选期间或数字线；修复身份后仍不得推断全场大0.5、不得回放旧单。

前置条件为同一发布方稳定球队编号连接原名与供应方完整名。已核实捷报球队7429公开Summary页标题“迈拉索尔SP”、描述“米拉索俱乐部”，球队数据team4同一7429为米拉索/Mirassol；两个名称直接来自同一俱乐部记录。登记仅football/巴西甲级联赛完整名组，不泛删SP，不使用市镇页面推断俱乐部，不影响女子/青年/预备队、巴乙、州联赛或虚拟足球。

## 可复核来源

- https://info.nowscore.com/cn/team/Summary/7429.html 同一球队页标题迈拉索尔SP，描述米拉索俱乐部，Keywords标巴西甲。2026-11-14响应SHA256 fecda1b06f66e5195b4fbad08be541baebf9f394b67516ae47fb35a8ab6f640e。标题模板含“英超排名”，不将模板文案作为联赛证据。
- https://info.nowscore.com/jsData/teamInfo/team4.js?version=2026111505 同一球队7429=米拉索/Mirassol，联盟4=巴西甲/Brazil Serie A，另有球队343米内罗竞技；SHA256 af13e37bc535a56a6285a84bdfd3a302c0c1032d7c4a4b88aefe733f99996568。
- https://ctc-live.7m.com.cn/datafile/fgb.js 同一比赛5006542/球队7543米拉索尔，对手米内罗竞技，联赛巴甲；SHA256 68ff40017ef0a2666f8496a00da3b14782e0b0cdf02709126923ceac3afd4ed1。同编号英文 https://ctc-live.7m.com.cn/datafile/fen.js 为Mirassol FC/Atletico Mineiro (MG)/BRA D1；SHA256 4a41de424240b74fb1982d32344235089cb7d75051b6fc357607a9b31a09c84a。
- https://www.fctokyo.co.jp/zh-cn/news/details/329326/ 为FC东京官方2026-01-21公告，中文米拉索FC及英文Mirassol FC，提供独立俱乐部身份交叉核对；SHA256 f244e65982bd05e9f4c4f0ef0f142130e3deb2679b507fd2df7edb4f31f92ef8。

百科市镇“迈拉索尔/Mirassol”不是俱乐部同名证明，已排除。猜测资料路径、搜索空响应及被拒绝页面不作证据。登记原完整名及巴西甲/巴甲精确简称，不作相似字符串匹配。

## 实现与验收

新增已核验整名组及精确联赛标签；测试正常原名、无联赛全量候选、项目/联赛/梯队拒绝、虚拟同对阵隔离、重复真实对阵歧义及无信号副作用。冻结实际工具API镜像后验证，失败不发布；通过后只空闲切换工具API，实际健康及镜像核对后记录修复时间并重新累计完整30分钟窗口。没有真实资金测试或生产假消息。
