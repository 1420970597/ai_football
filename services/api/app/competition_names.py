"""Verified whole-name competition aliases, scoped to their sport."""
from __future__ import annotations

from .team_matching import normalize_team_name

_FOOTBALL_ALIASES = {
    '国际友谊': '国际友谊赛',
    '葡u23': '葡萄牙联赛U23',
    '葡萄牙u23联赛': '葡萄牙联赛U23',
    '印度甲': '印度甲级联赛',
    '巴西乙': '巴西乙级联赛',
    '巴西甲': '巴西甲级联赛',
    '巴甲': '巴西甲级联赛',
    '巴青杯': '巴西杯U20',
    '哥伦甲': '哥伦比亚甲级联赛',
    '哥伦比亚甲组联赛': '哥伦比亚甲级联赛',
    '拉脱超降': '拉脱维亚超级联赛 - 附加赛',
    '澳女联': '澳大利亚女子甲级联赛',
    '澳锦标': '澳大利亚锦标赛',
    '日职丙': '日本J3联赛',
    '日联杯': '日本职业联赛杯',
    '港超联杯': '香港联赛杯',
    '英超': '英格兰超级联赛',
    '英冠': '英格兰冠军联赛',
    '西甲': '西班牙甲级联赛',
    '西乙': '西班牙乙级联赛',
    '西班牙乙组联赛': '西班牙乙级联赛',
    '西班牙乙组足球联赛': '西班牙乙级联赛',
    '西女超': '西班牙女子甲级联赛',
    '挪超': '挪威超级联赛',
    '罗马尼亚甲组联赛': '罗马尼亚甲级联赛',
    '丹麦甲组联赛': '丹麦甲级联赛',
    '葡萄牙甲组联赛': '葡萄牙甲级联赛',
    '泰u21': '泰国联赛U21',
    '泰国u21': '泰国联赛U21',
    'thai u21': '泰国联赛U21',
    'thailand u21': '泰国联赛U21',
    '马统杯': '马来西亚总统杯U20',
}


def normalize_competition_name(value: str, sport: str | None) -> str:
    normalized = normalize_team_name(value)
    if sport == 'football':
        return normalize_team_name(_FOOTBALL_ALIASES.get(normalized, normalized))
    return normalized


def competition_query_members(value: str, sport: str | None) -> tuple[str, ...]:
    """An umbrella source label covers only its documented adult divisions.

    This is a query scope, never a canonical identity shared by divisions.
    Women's, youth, virtual and unknown divisions do not belong to this set.
    """
    if sport == 'football' and normalize_team_name(value) == '欧国联':
        return tuple(normalize_team_name('欧洲国家联赛'+division) for division in 'ABCD')
    if sport == 'football' and normalize_team_name(value) == '哥伦甲秋':
        # Publisher league 250, subleague 364 (Clausura). The native feed
        # exposes only the parent league; this is retrieval scope, not a
        # fabricated native season/stage or a team identity by itself.
        return (normalize_team_name('哥伦比亚甲级联赛'),)
    return ()


def competition_query_matches(query: str, candidate: str, sport: str | None) -> bool:
    members = competition_query_members(query, sport)
    canonical = normalize_competition_name(candidate, sport)
    return canonical in members if members else canonical == normalize_competition_name(query, sport)
