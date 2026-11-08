"""Conservative current-stage claims: a negative check, never binding evidence."""
from __future__ import annotations

import re
import unicodedata

_IDS = re.compile(r'(?<![A-Za-z0-9_:])leyu:[0-9]{1,24}(?![A-Za-z0-9_:])')
_PHASES = {
    'scheduled': 'scheduled', '赛前': 'scheduled', '未开赛': 'scheduled',
    'live': 'live', '滚球': 'live', '进行中': 'live',
    'finished': 'finished', '已完场': 'finished', '已结束': 'finished',
    'cancelled': 'cancelled', '取消': 'cancelled', '延期': 'cancelled',
    '取消/延期': 'cancelled',
}
_VALUES = '|'.join(sorted(map(re.escape, _PHASES), key=len, reverse=True))
_EXPLICIT = re.compile(
    rf'(?:(?:当前(?:系统)?(?:比赛)?|系统)(?:阶段|状态)|phase|status)'
    rf'(?:\s*[:=为是]\s*|\s+)({_VALUES})(?![A-Za-z0-9_\u4e00-\u9fff])', re.I)
_ISOLATED = re.compile(rf'(?:[,;|])\s*({_VALUES})\s*(?=[,;|。.]|$)', re.I)
_PAIRED = re.compile(
    rf'(?<![A-Za-z0-9_\u4e00-\u9fff])({_VALUES})'
    rf'(?:\s*\(\s*|\s+)({_VALUES})(?![A-Za-z0-9_\u4e00-\u9fff])', re.I)
_HISTORICAL = re.compile(r'原文|引用|推荐时|当时|历史|截图|source_intent', re.I)
_HISTORICAL_PREFIX = re.compile(r'^(?:原文|引用|推荐时|当时|历史|source_intent)', re.I)


def single_fixture_id(text: str) -> str | None:
    fixtures = set(_IDS.findall(text))
    return next(iter(fixtures)) if len(fixtures) == 1 else None


def current_phase_claims(content: str) -> list[dict[str, str]]:
    """Only a single explicit fixture ID per nonquoted line is attributable."""
    claims = set()
    fenced = False
    for raw in content[:20000].splitlines():
        line = unicodedata.normalize('NFKC', raw).strip()
        if line.startswith(('```', '~~~')):
            fenced = not fenced
            continue
        if fenced or line.startswith('>') or _HISTORICAL_PREFIX.search(line):
            continue
        line = re.sub(r'“[^”]*”|「[^」]*」', '', line)
        line = line.replace('**', '').replace('`', '')
        fixture = single_fixture_id(line)
        if fixture is None:
            continue
        patterns = (_EXPLICIT,) if _HISTORICAL.search(line) else (_EXPLICIT, _ISOLATED, _PAIRED)
        for pattern in patterns:
            for match in pattern.finditer(line):
                for value in match.groups():
                    claims.add((fixture, _PHASES[value.lower()]))
    return [{'fixture_id': fixture, 'claimed_phase': phase} for fixture, phase in sorted(claims)]
