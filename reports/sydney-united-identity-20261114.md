# 悉尼联身份简称核验

核验时间2026-11-14 05:28–05:30 UTC。真实推荐来源用“悉尼联”，比赛事实供应方用“悉尼联58”，工具严守完整身份未通过，模型重复同样resolve，单条触发20秒截止。不能用唯一候选或可售盘口取代身份证据。

证据均在本项目Docker内HTTP只读取得，无秘密或专家原文：

|来源|可核验事实|响应SHA256|
|---|---|---|
|https://data.7m.com.cn/team_data/7720/gb/index.shtml|标题为悉尼联(Sydney United)，团队编号7720|eefff72a71e30c99f1da5fe834b2fb4c8daa60ea7e196d5eec97e4363a27eb10|
|https://data.7m.com.cn/matches_data/1903/gb/matches.js|编号7720的悉尼联于2026-11-14 12:00（页面时间）主场对南墨尔本；前后赛程亦同队|fb84f451bc2236d2d735acf8aad16bddd373c873ad7bbc57b8ff6e5a0b9631f4|
|https://www.sydneyunited58fc.com/history|官方站标题Sydney United 58 FC，成立年份1958|2cd322b6a0ac6f427937feab5e0e15037003c436f286fc0ebad814d932177179|

当时持久比赛投影只读核验：足球、澳大利亚锦标赛、悉尼联58对南墨尔本、2026-11-14 04:00 UTC，主队供应方编号3246，客队3194。比赛日、主客和对手与7M赛程一致。此跨来源证据仅支持这两个完整中文名在该联赛同队，不证明其它悉尼球队或梯队身份。

实现只增加sport=football、competition_name精确为澳大利亚锦标赛的“悉尼联”与“悉尼联58”词汇。U20、女队、后备、悉尼FC、其它联赛和其它项目均不扩展；仍需完整候选分页、双方、时间、阶段及唯一性。推荐价只来自当前事实报价。

Docker29项5.782秒退出0，证据.runtime/test-evidence/4190253/backend-tests.json；含隔离API完整检索后一次绑定、不创建信号、梯队与跨范围拒绝。
