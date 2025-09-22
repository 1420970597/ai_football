import requests
import json
import time
import os
from datetime import datetime

def get_match_analysis(match_id):
    """
    获取比赛分析数据的所有API，返回完整的原始数据
    """
    analysis_apis = {
        "历史交锋": f"https://webapi.sporttery.cn/gateway/uniform/football/getResultHistoryV1.qry?sportteryMatchId={match_id}&termLimits=10&tournamentFlag=0&homeAwayFlag=0",
        "积分榜": f"https://webapi.sporttery.cn/gateway/uniform/football/getMatchTablesV2.qry?gmMatchId={match_id}",
        "未来比赛": f"https://webapi.sporttery.cn/gateway/uniform/football/getFutureMatchesV1.qry?sportteryMatchId={match_id}&termLimits=4",
        "射手信息": f"https://webapi.sporttery.cn/gateway/uniform/football/getMatchPlayerV1.qry?sportteryMatchId={match_id}&termLimits=3",
        "伤停信息": f"https://webapi.sporttery.cn/gateway/uniform/football/getInjurySuspensionV1.qry?sportteryMatchId={match_id}",
        "数据统计": f"https://webapi.sporttery.cn/gateway/uniform/football/getMatchFeatureV1.qry?termLimits=10&sportteryMatchId={match_id}"
    }

    analysis_data = {}
    print(f"    正在获取比赛{match_id}的详细分析数据...")

    for name, url in analysis_apis.items():
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
            # 保存完整的原始数据，不进行任何过滤
            analysis_data[name] = data
            time.sleep(0.2)  # 避免请求过于频繁
            print(f"      成功获取{name}数据")
        except Exception as e:
            print(f"      获取{name}数据失败: {e}")
            analysis_data[name] = {"错误": str(e), "数据状态": "获取失败"}

    return analysis_data

def translate_analysis_fields(analysis_data):
    """
    将详细分析数据的所有字段转换为中文，直接返回数据内容
    """
    translated_data = {}

    for category, data in analysis_data.items():
        if isinstance(data, dict) and "value" in data and data.get("success", False):
            # 直接使用翻译后的value内容，去掉API包装
            translated_data[category] = translate_value_content(data.get("value", {}), category)
        elif isinstance(data, dict) and "错误" in str(data):
            # 如果有错误，保留错误信息
            translated_data[category] = data
        else:
            # 其他情况直接翻译
            translated_data[category] = translate_any_data_structure(data)

    return translated_data

def translate_common_fields(field_name):
    """翻译常见字段名称"""
    field_map = {
        # 基础字段
        "teamName": "球队名称",
        "teamId": "球队ID",
        "playerId": "球员ID",
        "playerName": "球员姓名",
        "matchId": "比赛ID",
        "matchDate": "比赛日期",
        "leagueName": "联赛名称",
        "leagueId": "联赛ID",
        "teamShortName": "球队简称",
        "personName": "球员姓名",
        "personId": "球员ID",
        "uniformNo": "球衣号码",

        # 历史交锋字段
        "awayTeamFullCourtGoalCnt": "客队全场进球数",
        "awayTeamId": "客队ID",
        "awayTeamShortName": "客队简称",
        "fullCourtGoal": "全场比分",
        "gmLeagueId": "联赛ID",
        "halfTimeGoal": "半场比分",
        "homeMatchResult": "主队比赛结果",
        "homeTeamFullCourtGoalCnt": "主队全场进球数",
        "homeTeamId": "主队ID",
        "homeTeamShortName": "主队简称",
        "seasonId": "赛季ID",
        "sportteryAwayTeamId": "体彩客队ID",
        "sportteryHomeTeamId": "体彩主队ID",
        "sportteryMatchId": "体彩比赛ID",
        "sportteryTournamentId": "体彩联赛ID",
        "teamMatchResult": "球队比赛结果",
        "totalTeamFullCourtGoalCnt": "总进球数",
        "tournamentId": "联赛ID_2",
        "tournamentShortName": "联赛简称",
        "uniformAwayTeamId": "统一客队ID",
        "uniformHomeTeamId": "统一主队ID",
        "uniformLeagueId": "统一联赛ID",
        "winningTeam": "获胜球队",
        "matchList": "比赛列表",
        "statistics": "统计信息",

        # 积分榜字段
        "ranks": "排名",
        "points": "积分",
        "matchPlayed": "已赛场次",
        "wins": "胜场",
        "draws": "平场",
        "loses": "负场",
        "goalsFor": "进球",
        "goalsAgainst": "失球",
        "goalDifference": "净胜球",
        "drawMatchCnt": "平局场次",
        "goalCnt": "进球数",
        "groupId": "组别ID",
        "groupName": "组别名称",
        "lossGoalCnt": "失球数",
        "lossGoalMatchCnt": "失球场次",
        "netGoal": "净胜球",
        "phaseName": "阶段名称",
        "ranking": "排名",
        "totalLegCnt": "总场次",
        "uniformTeamId": "统一球队ID",
        "wbsjTeamId": "球队ID_2",
        "winGoalMatchCnt": "胜球场次",
        "winProbability": "胜率",
        "awayTables": "客队积分榜",
        "homeTables": "主队积分榜",
        "total": "总计",
        "away": "客场",
        "home": "主场",
        "leagueShortName": "联赛简称",
        "phaseId": "阶段ID",
        "seasonName": "赛季名称",
        "uniformMatchId": "统一比赛ID",

        # 未来比赛字段
        "matchDateTime": "比赛时间",
        "gameweek": "轮次",
        "sportteryTeamId": "体彩球队ID",

        # 射手字段
        "goals": "进球数",
        "assists": "助攻数",
        "position": "位置",
        "appearances": "出场次数",
        "appearanceCnt": "出场次数",
        "assistAvgCnt": "平均助攻",
        "assistCnt": "助攻数",
        "assistProbability": "助攻概率",
        "goalAvgCnt": "平均进球",
        "goalCnt": "进球数",
        "goalProbability": "进球概率",
        "injuryFlag": "伤病标志",
        "playerPositionCode": "位置代码",
        "playerPositionDesc": "位置描述",
        "startedMatchCnt": "首发场次",
        "substituteMatchCnt": "替补场次",
        "suspensionFlag": "停赛标志",
        "playerList": "球员列表",

        # 伤停字段
        "injuryType": "伤停类型",
        "reason": "原因",
        "expectedReturn": "预计恢复",
        "status": "状态",
        "injuriesAndSuspensionsList": "伤停名单",

        # 统计字段
        "recentForm": "近期状态",
        "winRate": "胜率",
        "avgGoalsFor": "平均进球",
        "avgGoalsAgainst": "平均失球",
        "homeWinRate": "主场胜率",
        "awayWinRate": "客场胜率",
        "possession": "控球率",
        "shots": "射门次数",
        "shotsOnTarget": "射正次数",
        "awayDrawMatchCnt": "客场平局场次",
        "awayLossGoalMatchCnt": "客场失球场次",
        "awayScoreRatio": "客场得分率",
        "awayWinGoalMatchCnt": "客场进球场次",
        "homeDrawMatchCnt": "主场平局场次",
        "homeLossGoalMatchCnt": "主场失球场次",
        "homeScoreRatio": "主场得分率",
        "homeWinGoalMatchCnt": "主场进球场次",
        "awayGoalAvgCnt": "客场平均进球",
        "awayGoalAvgCntRatio": "客场进球占比",
        "homeGoalAvgCnt": "主场平均进球",
        "homeGoalAvgCntRatio": "主场进球占比",
        "awayLossGoalAvgCnt": "客场平均失球",
        "awayLossGoalAvgCntRatio": "客场失球占比",
        "homeLossGoalAvgCnt": "主场平均失球",
        "homeLossGoalAvgCntRatio": "主场失球占比",
        "eachHomeAway": "主客场数据",
        "eachSameHomeAway": "相同主客场数据",
        "goalAvg": "进球平均数",
        "last": "近期数据",
        "lossGoalAvg": "失球平均数",
        "sameHomeAway": "相同主客场",

        # 比赛字段
        "homeTeamName": "主队名称",
        "awayTeamName": "客队名称",
        "homeTeam": "主队",
        "awayTeam": "客队",
        "guestTeam": "客队",
        "hostTeam": "主队"
    }

    return field_map.get(field_name, field_name)

def translate_any_data_structure(data, prefix=""):
    """通用数据结构翻译函数"""
    if isinstance(data, dict):
        translated = {}
        for key, value in data.items():
            chinese_key = translate_common_fields(key)
            if prefix:
                chinese_key = f"{prefix}_{chinese_key}"

            if isinstance(value, (dict, list)):
                translated[chinese_key] = translate_any_data_structure(value, "")
            else:
                translated[chinese_key] = value
        return translated
    elif isinstance(data, list):
        return [translate_any_data_structure(item, prefix) for item in data]
    else:
        return data

def translate_value_content(value_data, category):
    """
    根据不同类别翻译value内容的字段，直接返回中文化的数据
    """
    if not isinstance(value_data, dict):
        return value_data

    # 直接进行翻译，不保留原始数据层级
    try:
        if category == "历史交锋":
            return translate_history_data(value_data)
        elif category == "积分榜":
            return translate_standings_data(value_data)
        elif category == "未来比赛":
            return translate_future_matches_data(value_data)
        elif category == "射手信息":
            return translate_players_data(value_data)
        elif category == "伤停信息":
            return translate_injuries_data(value_data)
        elif category == "数据统计":
            return translate_statistics_data(value_data)
        else:
            # 对于未知类型，使用通用翻译
            return translate_any_data_structure(value_data)
    except Exception as e:
        return {"翻译错误": str(e), "原始数据": value_data}

def translate_history_data(data):
    """翻译历史交锋数据"""
    translated_data = {}

    # 翻译比赛列表
    if "matchList" in data:
        translated_matches = []
        for match in data["matchList"]:
            translated_match = {
                "客队全场进球数": match.get("awayTeamFullCourtGoalCnt", ""),
                "客队ID": match.get("awayTeamId", ""),
                "客队简称": match.get("awayTeamShortName", ""),
                "全场比分": match.get("fullCourtGoal", ""),
                "联赛ID": match.get("gmLeagueId", ""),
                "半场比分": match.get("halfTimeGoal", ""),
                "主队比赛结果": match.get("homeMatchResult", ""),
                "主队全场进球数": match.get("homeTeamFullCourtGoalCnt", ""),
                "主队ID": match.get("homeTeamId", ""),
                "主队简称": match.get("homeTeamShortName", ""),
                "比赛日期": match.get("matchDate", ""),
                "比赛ID": match.get("matchId", ""),
                "赛季ID": match.get("seasonId", ""),
                "体彩客队ID": match.get("sportteryAwayTeamId", ""),
                "体彩主队ID": match.get("sportteryHomeTeamId", ""),
                "体彩比赛ID": match.get("sportteryMatchId", ""),
                "体彩联赛ID": match.get("sportteryTournamentId", ""),
                "球队比赛结果": match.get("teamMatchResult", ""),
                "总进球数": match.get("totalTeamFullCourtGoalCnt", ""),
                "联赛ID_2": match.get("tournamentId", ""),
                "联赛简称": match.get("tournamentShortName", ""),
                "统一客队ID": match.get("uniformAwayTeamId", ""),
                "统一主队ID": match.get("uniformHomeTeamId", ""),
                "统一联赛ID": match.get("uniformLeagueId", ""),
                "获胜球队": match.get("winningTeam", "")
            }
            translated_matches.append(translated_match)
        translated_data["比赛列表"] = translated_matches

    # 翻译统计信息
    if "statistics" in data:
        stats = data["statistics"]
        translated_data["统计信息"] = {
            "平局场次": stats.get("drawMatchCnt", ""),
            "平局概率": stats.get("drawProbability", ""),
            "进球数": stats.get("goalCnt", ""),
            "失球数": stats.get("lossGoalCnt", ""),
            "失球场次": stats.get("lossGoalMatchCnt", ""),
            "失败概率": stats.get("lossProbability", ""),
            "净胜球": stats.get("netGoal", ""),
            "体彩球队ID": stats.get("sportteryTeamId", ""),
            "球队ID": stats.get("teamId", ""),
            "球队简称": stats.get("teamShortName", ""),
            "总场次": stats.get("totalLegCnt", ""),
            "胜球场次": stats.get("winGoalMatchCnt", ""),
            "胜利概率": stats.get("winProbability", "")
        }

    # 翻译其他所有字段
    for key, value in data.items():
        if key not in ["matchList", "statistics"]:
            chinese_key = translate_common_fields(key)
            translated_data[chinese_key] = value

    return translated_data

def translate_standings_data(data):
    """翻译积分榜数据"""
    translated = {}

    # 翻译所有字段
    for key, value in data.items():
        chinese_key = translate_common_fields(key)

        if isinstance(value, dict):
            # 递归翻译嵌套字典
            translated_value = {}
            for sub_key, sub_value in value.items():
                chinese_sub_key = translate_common_fields(sub_key)
                if isinstance(sub_value, dict):
                    # 继续递归翻译
                    translated_sub_value = {}
                    for nested_key, nested_value in sub_value.items():
                        chinese_nested_key = translate_common_fields(nested_key)
                        translated_sub_value[chinese_nested_key] = nested_value
                    translated_value[chinese_sub_key] = translated_sub_value
                else:
                    translated_value[chinese_sub_key] = sub_value
            translated[chinese_key] = translated_value
        else:
            translated[chinese_key] = value

    return translated

def translate_future_matches_data(data):
    """翻译未来比赛数据"""
    return translate_any_data_structure(data)

def translate_players_data(data):
    """翻译射手信息数据"""
    return translate_any_data_structure(data)

def translate_injuries_data(data):
    """翻译伤停信息数据"""
    return translate_any_data_structure(data)

def translate_statistics_data(data):
    """翻译数据统计"""
    return translate_any_data_structure(data)

def convert_to_chinese_fields_new(match, analysis_data, business_date):
    """
    将新接口的比赛数据转换为中文格式，保留完整信息
    """
    # 提取赔率信息
    odds_info = {}
    odds_list = match.get('oddsList', [])
    for odds in odds_list:
        pool_code = odds.get('poolCode', '')
        if pool_code == 'HAD':  # 胜平负
            odds_info.update({
                "主胜赔率": odds.get('h', ''),
                "平局赔率": odds.get('d', ''),
                "客胜赔率": odds.get('a', ''),
                "胜平负格式": f"{odds.get('h', 'N/A')} / {odds.get('d', 'N/A')} / {odds.get('a', 'N/A')}"
            })
        elif pool_code == 'HHAD':  # 让球胜平负
            odds_info.update({
                "让球主胜赔率": odds.get('h', ''),
                "让球平局赔率": odds.get('d', ''),
                "让球客胜赔率": odds.get('a', ''),
                "让球盘口": odds.get('goalLine', ''),
                "让球胜平负格式": f"{odds.get('h', 'N/A')} / {odds.get('d', 'N/A')} / {odds.get('a', 'N/A')}"
            })

    # 构建中文数据结构
    chinese_match_data = {
        "基本信息": {
            "场次号": match.get('matchNum'),
            "场次编号字符串": match.get('matchNumStr'),
            "比赛日期": match.get('matchDate'),
            "比赛时间": match.get('matchTime'),
            "业务日期": business_date,
            "星期": match.get('weekday'),
            "联赛名称": match.get('leagueAllName'),
            "联赛简称": match.get('leagueAbbName'),
            "联赛ID": match.get('leagueId'),
            "主队名称": match.get('homeTeamAbbName'),
            "主队全称": match.get('homeTeamAllName'),
            "主队ID": match.get('homeTeamId'),
            "客队名称": match.get('awayTeamAbbName'),
            "客队全称": match.get('awayTeamAllName'),
            "客队ID": match.get('awayTeamId'),
            "比赛ID": match.get('matchId'),
            "比赛状态": match.get('matchStatus'),
            "销售状态": match.get('sellStatus'),
            "背景色": match.get('backColor'),
            "备注": match.get('remark', '')
        },
        "赔率信息": odds_info,
        "玩法信息": {
            "可用玩法": []
        },
        "详细分析数据": translate_analysis_fields(analysis_data),
        "数据获取信息": {
            "获取时间": datetime.now().isoformat(),
            "数据来源": "中国体育彩票官方API V2",
            "接口版本": "getMatchListV1",
            "数据完整性": "包含所有爬虫获取的原始信息"
        }
    }

    # 添加玩法池信息
    pool_list = match.get('poolList', [])
    for pool in pool_list:
        pool_code = pool.get('poolCode', '')
        pool_info = {
            "玩法代码": pool_code,
            "玩法状态": pool.get('poolStatus', ''),
            "单关投注值": pool.get('intSingle', 0),
            "过关投注值": pool.get('intAllUp', 0),
            "复式单关值": pool.get('cbtSingle', 0),
            "复式过关值": pool.get('cbtAllUp', 0)
        }

        # 根据玩法代码添加中文名称
        pool_names = {
            'HAD': '胜平负',
            'HHAD': '让球胜平负',
            'TTG': '总进球',
            'HAFU': '半全场',
            'CRS': '比分'
        }
        pool_info["玩法名称"] = pool_names.get(pool_code, pool_code)
        chinese_match_data["玩法信息"]["可用玩法"].append(pool_info)

    return chinese_match_data

def convert_to_chinese_fields(match, analysis_data, lottery_info):
    """
    将所有字段转换为中文格式，保留完整信息
    """
    chinese_match_data = {
        "基本信息": {
            "场次号": match.get('matchNum'),
            "联赛名称": match.get('matchName'),
            "联赛ID": match.get('leagueId'),
            "主队名称": match.get('masterTeamName'),
            "主队全称": match.get('masterTeamAllName'),
            "客队名称": match.get('guestTeamName'),
            "客队全称": match.get('guestTeamAllName'),
            "比赛日期": match.get('startTime'),
            "比赛ID": match.get('infohubMatchId'),
            "比赛结果": match.get('result', ''),
            "半场比分": match.get('czHalfScore', ''),
            "全场比分": match.get('czScore', '')
        },
        "赔率信息": {
            "主胜赔率": match.get('h'),
            "平局赔率": match.get('d'),
            "客胜赔率": match.get('a'),
            "胜平负格式": f"{match.get('h', 'N/A')} / {match.get('d', 'N/A')} / {match.get('a', 'N/A')}"
        },
        "期次信息": {
            "期次号": lottery_info.get('lotteryDrawNum'),
            "游戏名称": lottery_info.get('lotteryGameName'),
            "游戏编号": lottery_info.get('lotteryGameNum'),
            "销售开始时间": lottery_info.get('lotterySaleBegintime'),
            "销售截止时间": lottery_info.get('lotterySaleEndtime'),
            "开奖时间": lottery_info.get('lotteryDrawTime'),
            "预计开奖时间": lottery_info.get('estimateDrawTime')
        },
        "详细分析数据": translate_analysis_fields(analysis_data),
        "数据获取信息": {
            "获取时间": datetime.now().isoformat(),
            "数据来源": "中国体育彩票官方API",
            "数据完整性": "包含所有爬虫获取的原始信息"
        }
    }

    return chinese_match_data

def get_football_data_single_files():
    """
    获取足球彩票数据并为每场比赛生成单独的JSON文件
    根据当前时间判断获取当天还是第二天的比赛数据
    """
    url = "https://webapi.sporttery.cn/gateway/uniform/football/getMatchListV1.qry"
    params = {
        "clientCode": "3001"
    }

    try:
        print("正在获取足球彩票基础数据...")
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()

        if "value" in data and "matchInfoList" in data["value"]:
            match_info_list = data["value"]["matchInfoList"]

            # 判断当前时间，决定获取哪一天的数据
            current_time = datetime.now()
            current_hour = current_time.hour
            current_date = current_time.strftime("%Y-%m-%d")

            # 寻找匹配当前日期的索引
            target_index = 0
            today_index = None
            tomorrow_index = None

            # 寻找今天和明天的数据索引
            for i, match_info in enumerate(match_info_list):
                business_date = match_info.get("businessDate", "")
                if business_date == current_date:
                    today_index = i
                elif business_date > current_date:
                    if tomorrow_index is None:  # 找到第一个未来日期
                        tomorrow_index = i

            # 根据时间和数据可用性决定获取哪个索引
            if current_hour >= 19:
                # 晚于19:00，优先获取明天的数据
                if tomorrow_index is not None:
                    target_index = tomorrow_index
                    target_date = match_info_list[tomorrow_index].get("businessDate", "")
                    print(f"当前时间为{current_time.strftime('%H:%M')}，晚于19:00，获取明天({target_date})的比赛数据")
                else:
                    target_index = today_index if today_index is not None else 0
                    target_date = match_info_list[target_index].get("businessDate", "")
                    print(f"当前时间为{current_time.strftime('%H:%M')}，晚于19:00，但未找到明天数据，获取{target_date}的比赛数据")
            else:
                # 早于19:00，优先获取今天的数据
                if today_index is not None:
                    target_index = today_index
                    target_date = match_info_list[today_index].get("businessDate", "")
                    print(f"当前时间为{current_time.strftime('%H:%M')}，早于19:00，获取今天({target_date})的比赛数据")
                else:
                    # 如果找不到今天的数据，获取最近的未来数据
                    target_index = tomorrow_index if tomorrow_index is not None else 0
                    target_date = match_info_list[target_index].get("businessDate", "")
                    print(f"当前时间为{current_time.strftime('%H:%M')}，早于19:00，但未找到今天数据，获取{target_date}的比赛数据")

            if target_index >= len(match_info_list):
                print(f"未找到目标日期的比赛数据，将使用第一天的数据")
                target_index = 0

            selected_match_info = match_info_list[target_index]
            match_list = selected_match_info.get("subMatchList", [])
            business_date = selected_match_info.get("businessDate", "未知")

            print(f"\n目标比赛日期：{business_date}")
            print(f"发现 {len(match_list)} 场比赛")

            # 确保output目录存在
            output_dir = "output"
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)

            # 清空output目录中的旧文件
            for file in os.listdir(output_dir):
                if file.endswith('.json'):
                    os.remove(os.path.join(output_dir, file))

            print(f"\n开始处理每场比赛并生成单独的JSON文件...")

            for i, match in enumerate(match_list, 1):
                print(f"\n=== 处理第{i}场比赛 ===")
                print(f"场次{match.get('matchNum')}: {match.get('homeTeamAbbName')} vs {match.get('awayTeamAbbName')}")

                # 获取比赛详细分析数据
                match_id = match.get('matchId')
                if match_id:
                    analysis_data = get_match_analysis(match_id)
                else:
                    print("    警告: 无法获取比赛ID，跳过详细分析")
                    analysis_data = {"错误": "无比赛ID"}

                # 转换为中文字段格式
                chinese_data = convert_to_chinese_fields_new(match, analysis_data, business_date)

                # 生成文件名：场次号_主队vs客队_日期.json
                home_team = match.get('homeTeamAbbName', 'unknown')
                away_team = match.get('awayTeamAbbName', 'unknown')
                match_num = match.get('matchNum', 'unknown')
                filename = f"场次{match_num}_{home_team}vs{away_team}_{business_date}.json"

                # 处理文件名中的特殊字符
                filename = filename.replace('/', '_').replace('\\', '_').replace(':', '_').replace('*', '_').replace('?', '_').replace('"', '_').replace('<', '_').replace('>', '_').replace('|', '_')

                filepath = os.path.join(output_dir, filename)

                # 保存JSON文件
                try:
                    with open(filepath, 'w', encoding='utf-8') as f:
                        json.dump(chinese_data, f, ensure_ascii=False, indent=2)
                    print(f"    已保存: {filename}")
                except Exception as e:
                    print(f"    保存失败: {e}")

            print(f"\n处理完成!")
            print(f"共生成 {len(match_list)} 个JSON文件")
            print(f"文件保存位置: {os.path.abspath(output_dir)}")

            return True

        else:
            print("未找到有效的比赛数据")
            return False

    except Exception as e:
        print(f"获取数据时发生错误: {e}")
        return False

if __name__ == "__main__":
    print("=== 足彩数据获取工具 - 单场比赛JSON输出 ===")
    print("所有字段已中文化，包含完整爬虫数据")
    print("输出位置: output文件夹")

    result = get_football_data_single_files()

    if result:
        print("\\n所有数据获取和保存完成!")
    else:
        print("\\n数据获取失败!")