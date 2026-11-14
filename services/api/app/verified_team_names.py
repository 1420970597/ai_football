"""Reviewed identity vocabulary; provenance lives beside the acceptance evidence.

Only reviewed whole labels with recorded identity evidence belong here.
Cross-publisher labels require an explicit evidence chain; adding a squad
suffix or another sport requires independent identity evidence.
"""

REGISTRY_VERSION = '2026-11-14.6'
VERIFIED_TEAM_NAMES = (
    {
        'sport':'football','competition':'印度甲级联赛',
        'names':('班加鲁鲁联','孟加拉鲁联合会','班加罗尔联合'),
        'reference':'titan:41334:bfdata+alias3+7m:632313:20261114',
    },
    {
        'sport':'football','competition':'拉脱维亚超级联赛 - 附加赛',
        'names':('瓦尔麦斯','瓦尔米耶拉'),
        'reference':'titan:14486:bfdata+alias3:20261114',
    },
    {
        'sport':'football','competition':'日本J3联赛',
        'names':('相模原','SC相模原'),
        'reference':'jleague:sagamihara:profile+7m:1294+scsagamihara:20261114',
    },
    {
        'sport':'football','competition':'日本J2联赛',
        'names':('新泻天鹅','新潟天鹅'),
        'reference':'7m:958+live:5079549+albirex:53216+leyu:3226:20261114',
    },
    {
        'sport':'football','competition':'日本J3联赛',
        'names':('温泉群马','群马草津温泉'),
        'reference':'7m:1927+live:5078648+thespa:20261114',
    },
    {
        'sport':'football','competition':'香港联赛杯',
        'names':('北区','高力北区'),
        'reference':'7m:21356+schedule:1764+leyu:32745:20261114',
    },
    {
        'sport':'football','competition':'香港联赛杯',
        'names':('九龙城','和富九龙城'),
        'reference':'7m:21379+schedule:1764+leyu:33414:20261114',
    },
    {
        'sport':'football','competition':'香港联赛杯',
        'names':('香港足球会','港会','FC香港'),
        'reference':'7m:4897+schedule:1764+hkfc+leyu:44569:20261114',
    },
    {
        'sport':'football','competition':'澳大利亚女子甲级联赛',
        'names':('中央海岸水手(女)','中央海岸水手学院(女)','中岸水手女足','中部海岸海员女足'),
        'reference':'7m:9047+live:5172881+leyu:297502:20261114',
    },
    {
        'sport':'football','competition':'澳大利亚女子甲级联赛',
        'names':('珀斯光荣女足','珀斯光荣(女)'),
        'reference':'7m:9046+live:5172881+leyu:34961:20261114',
    },
    {
        'sport':'football','competition':'澳大利亚锦标赛',
        'names':('悉尼联','悉尼联58'),
        'reference':'7m:7720+schedule:1903+leyu:3246:20261114',
    },
    {
        'sport': 'basketball', 'competition': 'nba',
        'names': ('国王', '萨克拉门托国王'),
        'reference': 'hupu:kings:20261114',
    },
    {
        'sport': 'basketball', 'competition': 'nba',
        'names': ('开拓者', '波特兰开拓者'),
        'reference': 'hupu:blazers:20261114',
    },
    {
        'sport': 'basketball', 'competition': 'nba',
        'names': ('快船', '洛杉矶快船'),
        'reference': 'hupu:clippers:20261114',
    },
    {
        'sport': 'basketball', 'competition': 'nba',
        'names': ('雷霆', '俄克拉荷马城雷霆'),
        'reference': 'hupu:thunder:20261114',
    },
    {
        'sport': 'football',
        'names': ('恰斯拉夫', '卡拉夫', 'FK Caslav'),
        'reference': '7m:4745:20261108',
    },
    {
        'sport': 'football',
        'names': ('伊普斯维奇', '葉士域治'),
        'reference': '7m:18:20261108',
    },
    {
        'sport': 'football',
        'names': ('伯恩茅斯', '般尼茅夫'),
        'reference': '7m:674:20261108',
    },
    {
        'sport': 'football',
        'names': ('根特', '真特'),
        'reference': '7m:7:20261108',
    },
    {
        'sport': 'football',
        'names': ('科特赖克', '哥積克'),
        'reference': '7m:1609:20261108',
    },
    {
        'sport': 'football',
        'names': ('卡森邦迪特大学U21', '博乐大学U21', '博乐大学(U21)',
                  'Kasem Bundit University(U21)', 'Kasem Bundit University U21'),
        'reference': '7m:750141+leyu:329173:20261110',
    },
    {
        'sport': 'football',
        'names': ('巴蜀基利U21', '普拉查普U21', '普拉查普(U21)', '巴蜀(U21)',
                  'PT Prachuap FC(U21)', 'PT Prachuap U21', 'Prachuap U21'),
        'reference': '7m:750221+leyu:330276:20261110',
    },
    {
        'sport': 'basketball',
        'names': ('高阳天空枪手', '高阳索诺天枪', 'Goyang Sono Skygunners'),
        'reference': 'easl+publisher:210162+leyu:24449:20261110',
    },
    {
        'sport': 'basketball',
        'names': ('东京太阳摇滚', '涩谷太阳摇滚', 'Tokyo Sunrockers', 'Sunrockers Shibuya'),
        'reference': 'sunrockers:2026-27+publisher:211465+leyu:26100:20261110',
    },
    {
        'sport': 'football',
        'names': ('柔佛达鲁塔克欣C队U20', '达罗塔晋C队U20', '柔佛新山C队(U20)',
                  'Johor Darul Takzim C(U20)'),
        'reference': '7m:749715+titan:3111931+leyu:276426:20261110',
    },
)
