"""Verified whole-name competition aliases, scoped to their sport."""
from __future__ import annotations

from .team_matching import normalize_team_name

_FOOTBALL_ALIASES = {
    '英超': '英格兰超级联赛',
    '英冠': '英格兰冠军联赛',
    '西甲': '西班牙甲级联赛',
}


def normalize_competition_name(value: str, sport: str | None) -> str:
    normalized = normalize_team_name(value)
    if sport == 'football':
        return normalize_team_name(_FOOTBALL_ALIASES.get(normalized, normalized))
    return normalized
