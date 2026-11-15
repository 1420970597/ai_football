# 柏雷素尔完整译名核验

2026-11-15，Docker现有egress内只读HTTP核验，未使用比赛比分或可售盘口推定队伍身份。

- 捷报同编号209列表 `https://info.nowscore.com/jsData/teamInfo/team25.js` 一行同时列柏太阳神、柏雷素爾、Kashiwa Reysol；响应SHA256 `98369b36105c24c5db7533776d7663ecc1335755d59d136aa425766d6508d223`。
- 同编号详情 `https://info.nowscore.com/jsData/teamInfo/teamDetail/tdl209.js` 再次同时列上述三名及官方reysol.co.jp，响应SHA256 `3255e130d2298ba98522c488d2e38e52a7380f4f09186c207cd047d2a801d8a6`。详情含无关Kanazawa字段，未用于身份依据。
- J联赛官方 `https://www.jleague.jp/club/kashiwa/profile/` 返回柏レイソル官方俱乐部页，SHA256 `8e4f30e9398b6fbe4ea496d6792a1afea065bfe8ff9aeb11486e0b93cae295cf`，作为原俱乐部参考。
- 同出版方联赛索引72=日联杯；线上原始双方窗口候选为日本职业联赛杯、柏太阳神对川崎前锋。简称只登记足球，球队组只登记这一本次已核验联赛；女队、青年、B队与其它体育不合并。
- 其他疑似译名（斯拉斯克女足/弗罗茨瓦夫女足等）尚未取得完整出版方链，不加入注册表；对外请求部分ConnectTimeout/网络隔离ConnectError保留，不关闭TLS、不使用模糊匹配。
