"""Reviewed identity vocabulary; provenance lives beside the acceptance evidence.

Only reviewed whole labels with recorded identity evidence belong here.
Cross-publisher labels require an explicit evidence chain; adding a squad
suffix or another sport requires independent identity evidence.
"""

REGISTRY_VERSION = '2026-11-14.1'
VERIFIED_TEAM_NAMES = (
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
