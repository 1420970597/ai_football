# 澳女联与中央海岸水手女足身份核验

更新时间：2026-11-14 06:30 UTC。目标：修复自然原文明确女足、半场大0.75，但检索澳女联返回0、后续重复解析超过20秒的失败。原记录保留失败，不重放信号或订单。

## 证据与范围

- 官方俱乐部女足赛程 https://ccmariners.com.au/a-league-women/matches/ 返回200，SHA256 899b3cd6c9b065c21d6ba71c1f360961aed8392b5677c787935287760536db28；页面数据明确Ninja A-League Women、女足队编号c535liylsox5safxy4qwerxx8，独立于男队。
- 同页公开使用的 https://match.api.aleagues.football/middleware/v2/fixturesAndResultsV2 ，按页面公开competitionId=9p3nnxhdjahfn8qswpzy8oyc3、tournamentId=di1eymkpweljbeck80fwsuiok、teamId=c535liylsox5safxy4qwerxx8请求，200，SHA256 e2dba2459753715bf0976e802ffe4661f8882eb28da6b015f0982b76547e931b；aqp5ywzdl3kismxa052rdxlw4为Central Coast Mariners主场对Perth Glory，ALW，startDate=1794636000000，即2026-11-14 06:00 UTC。未使用凭据或绕过权限。
- 7M https://ctc-live.7m.com.cn/datafile/fgb.js 返回200，SHA256 9ea34eb64db37bd0b8d673cd14d948215ca462944c441eafbe7815e4a86bb754；sDt[5172881]明确澳女联、中央海岸水手女足/珀斯光荣女足、球队9047/9046、联赛765。入口来自其公开比分页soccer_f.aspx的脚本链接。
- https://data.7m.com.cn/matches_data/765/gb/index.shtml 标题明确澳女联(澳大利亚女子联赛)，200，SHA256 525fc1492000b92e23dfd412ffd84d56845427b0681baa54e082752d899fd6ad；https://data.7m.com.cn/team_data/9047/gb/index.shtml 标题Central Coast Mariners Women's，200，SHA256 9d8ea2343f3e14866915a0e9d2064135fd53c61f0bcf05a64b455664af233758。
- 乐鱼只读事实：leyu:5786012，football，联赛396/澳大利亚女子甲级联赛，主队297502/中央海岸水手学院(女)，客队34961/珀斯光荣(女)，开球06:00 UTC。官方同日主客与时间、独立出版方女足身份及联赛一致，支持这两个完整中文名在该联赛内等价。

## 实现决策

只增加足球完整简称澳女联到澳大利亚女子甲级联赛，以及该联赛内中央海岸水手(女)/中央海岸水手学院(女)两个完整名称。别名范围先复用已验证联赛归一，再比较；不去掉学院、女、U20、后备或其他联赛，不增加泛前缀。无独立身份来源的其他翻译暂不登记。

首轮原型测试发现范围比较未处理联赛简称，候选数仍0。该失败是真实实现缺陷；修复后重新冻结并验证首轮搜索、唯一绑定及防误配场景。另有错误测试模块和原文夹具问题均保留失败记录。

## 限制

7M matches.js读取到旧赛季同队赛程，不用作今日时间证据；今日时间来自官方实时赛程与乐鱼。公众来源只证明身份，不提供业务价格。报价仍必须来自乐鱼当前读取，20秒门禁、权限、分页与来源原文规则不变。
