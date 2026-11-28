# 巴西杯 U20 赛事与球队名称核验

2026 年 11 月 18 日 UTC。责任目录为 API 的有据球队别名、赛事名称及审核测试。目标是让现有审核按原始截图中的完整身份查询乐鱼当前赛事。

20:54 UTC 的自然审核使用了带比分的单行原图。原图写有「巴青杯」「欧帕尔利奥U20」和「帕尔梅拉斯青年队」。对照同一尝试的工具记录，四次球队搜索均没有返回候选，其中两次已去掉赛事简称限制。原图仅留于现有 72 小时媒体存储。

帕尔梅拉斯官网在赛前确认该队 U20 客战 Operário-PR，为巴西杯 U20 半决赛首回合，开球为 11 月 18 日 16:30 巴西利亚时间，即 19:30 UTC。赛后公告也确认同一对阵和赛事。赛前页面所取版本的 SHA-256 为 `cff60a8ee733b738aefee76efe60e283d9c402adbf73223d7174bfa6ac8fa21b`。 [帕尔梅拉斯赛前公告](https://www.palmeiras.com.br/noticias/palmeiras-visita-operario-pr-no-jogo-de-ida-da-semifinal-da-copa-do-brasil-sub-20/) [帕尔梅拉斯赛果公告](https://www.palmeiras.com.br/noticias/palmeiras-vence-operario-pr-e-sai-na-frente-por-vaga-na-final-da-copa-do-brasil-sub-20/)

乐鱼当前保留的同一赛事为 `leyu:5860383`。赛事名「巴西杯U20」，球队「铁路工人PR U20」和「帕尔梅拉斯U20」，编号分别是 `77176` 和 `24871`。双方、年龄组、赛事和开球时刻均对应。所取投影已完场，行中最新接收时点晚于原审核。历史搜索结果为空，无法从当前投影推定审核当时是否已有该行及可交易价格。

将「巴青杯」映射到足球「巴西杯U20」。两组译名严格限定该赛事，仍由现有规则分别校验双方、开球时间及阶段。定向 Docker `unittest` 16 项通过，包括跨项目、年龄限制、歧义及完场图片样本。完场样本可归档绑定，实时资格为否，隔离数据库中新推荐及订单均为零。

2026 年 11 月 18 日 22:16:39 UTC，仅独立 MCP API 切换到镜像 `sha256:c4249fd0c60d4f96337854705b2ff1b000df2c3c44e67fe058fb438b2737fb99`，容器健康且零重启。生产只读验证确认双方的来源标识、赛事缩写和成年队排除结果。截止 22:17 UTC 还未产生新版自然审核。回滚使用相同 Compose 文件、`ai-football-mcp-api:we-league-alias-20261118` 和同一服务。 [候选提交](https://github.com/1420970597/ai_football/commit/05add855891073aa22924912f6b56b0f6114378a)
