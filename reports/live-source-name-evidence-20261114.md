# 自然发言日港译名缺口核验

更新时间2026-11-14 06:41 UTC。06:00–06:30完整快照09a7f89e有多次明确文本因名称消歧超过20秒；保留原失败，禁止补写新鲜时间或重放下单。

## 可核对证据

公开比分入口 https://bf.7m.com.cn/soccer_f.aspx?encode=gb&view=All 加载 https://ctc-live.7m.com.cn/datafile/fgb.js 。本次响应SHA256 787ce84c0a94ffa8b3964d574c83b7b931784aa36f6d6b0d040e77b6282537ff，显示：

- 5079549：新泻天鹅/宫崎特格瓦雅罗，球队958/451958，日乙347；乐鱼5612557为新潟天鹅/特格瓦嘉洛宫崎，日本J2联赛、05:00UTC。官方 https://www.albirex.co.jp/game/53216 为2026/27 J2新潟vs宮崎，SHA256 c17dce240ac02de9505e2f4cd6301efabb7edef6ac81dec4d84ebce4116fcb0a；https://data.7m.com.cn/team_data/958/gb/index.shtml 标题新泻天鹅(Albirex Niigata)，SHA256 c0b7083c9655bbc8965ae1dc780490af3385830e2e71b50d402f46cfec269a87。只登记J2中新泻天鹅/新潟天鹅整名，不能影响同日新泻天鹅女足。
- 5078648：北九州向日葵/群马草津温泉，球队8962/1927，日丙1294；乐鱼5857594同名同主客、J3、05:00UTC。原图原文另一译名温泉群马；https://data.7m.com.cn/team_data/1927/gb/index.shtml 标题群马草津温泉(Thespa Kusatsu Gunma)，SHA256 7c78f3a95a666e99c912da42fb984f25b90b6553e3e7e3a5d9b2650ff0c00ca9，官方 https://www.thespa.co.jp/ 为THESPA GUNMA，SHA256 42806e5e09dd8a460ea7e77b360b20f9e6a22a99d0c84c0265478dd9a825dc66。只在J3内登记温泉群马/群马草津温泉，不做任意倒词规则。日职丙仅作为日本J3联赛完整简称。
- 5198766：高力北区/FC香港，球队21356/4897；5198768：和富九龙城/杰志，21379/3985；均港超联杯1764。乐鱼5842392及5842298分别对应高力北区/港会、和富九龙城/杰志，香港联赛杯、07:00UTC。https://data.7m.com.cn/matches_data/1764/gb/matches.js 的组1/组4明确2026,11,14,15,00,00及相同主客，SHA256 019d7a28056e3ea71ff122449e70703deb79c0f052d43bcc0e40ec1b249b8211（发布方UTC+8）。
- https://data.7m.com.cn/matches_data/1764/gb/index.shtml 标题港超联杯(香港超级联赛杯)，SHA256 4f48af2794f8b2ecd4a225d3b992eb1ea4d46542c8997010586ddccefc3bc6bb。https://data.7m.com.cn/team_data/21356/gb/index.shtml 标题北区(North District FC)，SHA256 a2c2749a77173f95cfa79792d13962fce6a1a9c3a485d95d4aa59c332297407a；21379标题九龙城(Kowloon City)，SHA256 6bd265dca10b9c836f74ecbb4292a8a8d20e5256df544214ff7a5b0d1452fcc0；4897标题FC香港(Hong Kong FC)，SHA256 7dd60b731045585def42fa2cba88826038816779abe132c51efcc75f8a4a1ab2。官方 https://www.hkfc.com/ 标题Hong Kong Football Club，SHA256 ec4ed8ca6e346509b0767b2004159d7e75c7b37717df68cc05d00a55a206b053；原文香港足球会指该完整名称，与同日对阵交叉一致。

## 实现与验收边界

07:34 UTC补充完整译名证据：7M香港页面 https://www.7m.hk/football/data/teams/9047 标题为中岸水手女足(Central Coast Mariners Women's)，同一出版方球队9047简体页面标题中部海岸海员女足，同一响应SHA256 9d8ea2343f3e14866915a0e9d2064135fd53c61f0bcf05a64b455664af233758；捷报 https://info.nowscore.com/big/team/Summary.aspx?teamid=9090 搜索索引同时给中央海岸水手(女)、粤语中岸水手女足、英文Central Coast Mariners(W)。仅并入既有女足组，不将中岸裸名用于男队。7M9046简体标题珀斯光荣女足(Perth Glory Women's)，响应582dc234891805220340e32a0da446e45568e9b966a1416268edb5ca9e3a870c；沿既有官方赛程/对阵证据连到珀斯光荣(女)。

日本J联赛官方 https://www.jleague.jp/club/sagamihara/profile/ 的クラブ名の由来明确SC是加在地名相模原前的スポーツクラブ，页面列J3和S.C.Sagamihara；俱乐部官方 https://www.scsagamihara.com/ 标题SC相模原，响应fb90766fe67428daac6178841eeccfdfbd74e6990458ec2eb493771260ec7398。7M J3赛程 https://data.7m.com.cn/matches_data/1294/gb/matches.js 响应bee34ae2eb641dfa7d602ff4d6fd625e620bd108b855f92675d52cc914e5f341含SC相模原。仅J3登记相模原/SC相模原整名，不建立泛删SC规则，U18/U15/FIORE女队仍拒绝。07:28样本候选已finished，身份识别后必须保留不可实时录入，不能回放新推荐。

06:56 UTC补充：原图“澳锦标”与供应方“澳大利亚锦标赛”联赛筛选不一致。出版方 https://data.7m.com.cn/matches_data/1903/gb/index.shtml UTF-8原始标题明确“澳锦标(澳大利亚锦标赛) 2026资料库”，响应SHA256 12d3b2daae6f02038e4e138e14d37ba2b1b1c1c65066d6fc800dda28b75dcaf1。只新增足球完整简称，篮球及加女足后缀仍不归一。首次按gb18030误解码已纠正，不以乱码证明名称。

只在足球指定联赛内登记五组完整队名与两个完整联赛简称。不得泛删赞助商、学院、U20、女足、后备；不同联赛不自动复用。原文不唯一或赔率无有效当前证据时仍拒绝。隔离数据库验证四组真实原文首轮完整搜索即可唯一绑定，且不生成推荐或订单；女足、U20、后备、其它项目及相似青年联赛负例保留。
