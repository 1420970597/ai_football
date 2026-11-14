"""Verified whole-name competition aliases, scoped to their sport."""
from __future__ import annotations

from .team_matching import normalize_team_name

_FOOTBALL_ALIASES = {
    '葡u23': '葡萄牙联赛U23',
    '葡萄牙u23联赛': '葡萄牙联赛U23',
    '印度甲': '印度甲级联赛',
    '拉脱超降': '拉脱维亚超级联赛 - 附加赛',
    '澳女联': '澳大利亚女子甲级联赛',
    '澳锦标': '澳大利亚锦标赛',
    '日职丙': '日本J3联赛',
    '港超联杯': '香港联赛杯',
    '英超': '英格兰超级联赛',
    '英冠': '英格兰冠军联赛',
    '西甲': '西班牙甲级联赛',
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
