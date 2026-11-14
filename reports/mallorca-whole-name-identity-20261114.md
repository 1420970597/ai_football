# 西乙马略卡与马洛卡完整名称核验

核验时间：2026-11-14 18:12 UTC。目标仅为自然TG图片中的“马略卡”与乐鱼足球/西班牙乙级联赛“马洛卡”建立可审查的同一俱乐部名称关系，不推断推荐期间、盘口、方向或实时价格。

## 身份证据

1. 中文维基百科俱乐部条目“皇家馬略卡體育會”，pageid=173214、revid=95081315，正文及信息框明确对应Real Club Deportivo Mallorca / RCD Mallorca，并明确简称馬略卡。API：https://zh.wikipedia.org/w/api.php?action=query&format=json&prop=extracts%7Crevisions&rvprop=ids&explaintext=1&redirects=1&titles=皇家馬略卡體育會 。实际联合查询原字节SHA256：9291ba9baef607879d54e958ba0bdd67a534f681ab6c257e0a33a4a54a2a3176。
2. 同站教练“格雷戈里奥·曼萨诺”条目pageid=3835775、revid=91707220，实际wikitext四处把显示标签“马洛卡”直接链接至同一“皇家馬略卡體育會”目标；这证明是俱乐部名，不是岛屿的别称。API：https://zh.wikipedia.org/w/api.php?action=parse&format=json&page=格雷戈里奥·曼萨诺&prop=wikitext%7Crevid ；SHA256：dff6bb09356a6d78391e80eba00787782a147a9cd10db71a7191647bf9b7237c。
3. 俱乐部官网https://www.rcdmallorca.es/en 实际HTTP200，结构化SportsTeam实体名RCD Mallorca，官网及社交账号一致；SHA256：5958e2c61fe933dde7608ee9e4d5f8da8beb9f7455d398c40a39ea02d1efb37a。官网仅独立佐证英文俱乐部身份，未声称官网提供上述中文异译名。

## 查询失败及不采用的证据

- 直接“马洛卡”百科查询重定向到马略卡岛，不能作为球队关系证据，未采用。
- 猜测的两个俱乐部标题不存在；百度百科拒绝访问，Titan球队页断连；7M猜测编号未得到本队证据，未采用。官网/zh不存在，未声称已获得中文官网证明。
- 搜索摘要只用于定位教练原页，最终采用原页的明确链接目标；不从比赛库存相似名称倒推身份。

## 实现边界与验收

- 只加入football/西班牙乙级联赛范围的完整名组：马洛卡、马略卡、馬略卡、RCD Mallorca、Real Club Deportivo Mallorca。不新增字符串模糊转换，不包含女队、B队、青年队或其它联赛。
- 缺联赛查询只扩展已核验的召回词汇，最终逐候选仍按其真实项目、联赛核验；两个同对阵候选仍歧义，不生成信号。
- 测试覆盖原文名首查、缺联赛首查、不同项目/联赛/梯队拒绝及同对阵多赛程拒绝。冻结实际工具API候选回归后才允许发布。旧原图审核与历史失败窗保持，不重放成新交易。
