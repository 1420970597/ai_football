"""Reviewed identity vocabulary; provenance lives beside the acceptance evidence.

Only reviewed whole labels with recorded identity evidence belong here.
Cross-publisher labels require an explicit evidence chain; adding a squad
suffix or another sport requires independent identity evidence.
"""

REGISTRY_VERSION = '2026-11-15.1'
VERIFIED_TEAM_NAMES = (
    {
        'sport':'football','competition':'日本职业联赛杯',
        'names':('柏太阳神','柏雷素尔','柏雷素爾','Kashiwa Reysol'),
        'reference':'nowscore:209:team25+tdl209+jleague:kashiwa:20261115',
    },
    {
        'sport':'football','competition':'巴西甲级联赛',
        'names':('迈拉索尔SP','米拉索','米拉索尔','Mirassol','Mirassol FC'),
        'reference':'nowscore:7429:Summary+team4:20261114+7m:7543:gb+en',
    },
    {
        'sport':'football','competition':'哥伦比亚甲级联赛',
        'names':('卡尔达斯十一人','云斯卡尔达斯','卡達斯','Once Caldas','Deportiva Once Caldas'),
        'reference':'nowscore:2206:team250+zhwiki:2843026:86174735+oncecaldas:20261114',
    },
    {
        'sport':'football','competition':'巴西乙级联赛',
        'names':('塞阿拉CE','塞阿拉','施亞拉','Ceara'),
        'reference':'nowscore:2108:team358+zhwiki:1478898:94607468:20261114',
    },
    {
        'sport':'football','competition':'巴西乙级联赛',
        'names':('累西腓体育PE','累西腓体育','利斯菲體育會','Sport Club do Recife'),
        'reference':'nowscore:4176:team358+zhwiki:2840273:90259115:20261114',
    },
    {
        'sport':'football','competition':'巴西乙级联赛',
        'names':('巴西雷加塔斯AL','巴西雷加塔斯','雷加塔斯巴西','CRB AL','Clube de Regatas Brasil'),
        'reference':'nowscore:3954:teamDetail+zhwiki:2882182:89956506+crboficial:20261114',
    },
    {
        'sport':'football','competition':'巴西乙级联赛',
        'names':('戈亚斯GO','戈亚斯','戈伊亚斯','戈亞斯','Goias'),
        'reference':'zhwiki:369022:95067626+nowscore:360:team358:20261114',
    },
    {
        'sport':'football','competition':'西班牙乙级联赛',
        'names':('马洛卡','马略卡','馬略卡','RCD Mallorca','Real Club Deportivo Mallorca'),
        'reference':'zhwiki:173214:95081315+3835775:91707220:linked-label+rcdmallorca:20261114',
    },
    {
        'sport':'football','competition':'荷兰乙级联赛',
        'names':('格拉夫夏普','迪加史卓普','De Graafschap'),
        'reference':'zhwiki:909129:92545336:redirect+degraafschap:20261114',
    },
    {
        'sport':'football','competition':'欧洲国家联赛B',
        'names':('波黑','波斯尼亚和黑塞哥维那','波斯尼亞和黑塞哥維那'),
        'reference':'zhwiki:522238:95172807+nfsbih:men:a-national-team:20261114',
    },
    {
        'sport':'football','competition':'欧洲国家联赛B',
        'names':('威尔士','威尔斯','威爾斯','Wales'),
        'reference':'zhwiki:193578:93417827+faw:20261114',
    },
    {
        'sport':'football','competition':'西班牙女子甲级联赛',
        'names':('特内里费体育(女)','科斯塔阿德赫特内里费(女)',
                 '科斯塔阿德赫特内里费 (女)','格拉纳迪拉特内里费苏女足',
                 '格拉纳迪拉特内里费女足','CD Tenerife Femenino','Costa Adeje Tenerife'),
        'reference':'titan:24725:bfdata+alias3+7m:23506+official:tfe-femenino:history:20261114',
    },
    {
        'sport':'football','competition':'西班牙女子甲级联赛',
        'names':('西班牙人(女)','西班牙人女足','爱斯宾奴(女)','愛斯賓奴女足'),
        'reference':'7m:6852:gb+big:20261114',
    },
    {
        'sport':'football','competition':'国际友谊赛',
        'names':('中国台湾','中国台湾(中)','中国台北','中华台北','中華台北','Chinese Taipei'),
        'reference':'7m:939:live5198357+team-profile+afc:ctfa+zhwiki:95505837:20261114',
    },
    {
        'sport':'football','competition':'香港联赛杯',
        'names':('南区足球会','冠忠南区'),
        'reference':'titan:20395:bfdata+alias3:20261114',
    },
    {
        'sport':'football','competition':'葡萄牙联赛U23',
        'names':('法伦斯U23','法伦斯 U23','法鲁人U23'),
        'reference':'7m:649044+scfarense:sub23+zhwiki:95093528+leyu:57869:20261114',
    },
    {
        'sport':'football','competition':'葡萄牙联赛U23',
        'names':('葡萄牙体育U23','葡萄牙体育 U23','里斯本竞技U23'),
        'reference':'7m:453008+titan:39834+sporting:sub23+leyu:23267:20261114',
    },
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
    {'sport':'basketball','competition':'nba','names':('骑士','克利夫兰骑士'),'reference':'hupu:cavaliers:20261114'},
    {'sport':'basketball','competition':'nba','names':('独行侠','达拉斯独行侠'),'reference':'hupu:mavericks:20261114'},
    {'sport':'basketball','competition':'nba','names':('灰熊','孟菲斯灰熊'),'reference':'hupu:grizzlies:20261114'},
    {'sport':'basketball','competition':'nba','names':('篮网','布鲁克林篮网'),'reference':'hupu:nets:20261114'},
    {'sport':'basketball','competition':'nba','names':('森林狼','明尼苏达森林狼'),'reference':'hupu:timberwolves:20261114'},
    {'sport':'basketball','competition':'nba','names':('公牛','芝加哥公牛'),'reference':'hupu:bulls:20261114'},
    {'sport':'basketball','competition':'nba','names':('热火','迈阿密热火'),'reference':'hupu:heat:20261114'},
    {'sport':'basketball','competition':'nba','names':('活塞','底特律活塞'),'reference':'hupu:pistons:20261114'},
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
