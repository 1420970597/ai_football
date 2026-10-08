"""Score-conditioned in-play probabilities and Asian stake payment weights.

The market-fit model is a research baseline, not an independently validated edge.
No HTTP, disk or language model calls occur here.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Tuple

from core.settlement import _score_of, split_line

MODEL_VERSION = 'live-poisson-market-v1'
REGULATION_SECONDS = 90 * 60
MAX_REMAINING_GOALS = 20
FIT_ITERATIONS = 18
VIRTUAL_PATTERN = re.compile(r'eafc|e-football|esoccer|e-soccer|fifa|虚拟|电竞|电子足球', re.I)


def competition_type(info: Mapping[str, Any]) -> str:
    text = ' '.join(str(info.get(k) or '') for k in ('league', 'home', 'away', 'sport'))
    if VIRTUAL_PATTERN.search(text):
        return 'virtual'
    return 'real' if str(info.get('sport_id') or '1') == '1' and info.get('league') else 'unknown'


def clock_seconds(raw: Any, period: Any) -> Optional[float]:
    """mst is cumulative seconds in observed regulation phases 6/7.

    A second-half value below 45:00 is ambiguous: do not add 45 speculatively.
    """
    if str(period) not in ('6', '7') or isinstance(raw, bool):
        return None
    try:
        if isinstance(raw, str) and ':' in raw:
            minute, second = raw.split(':', 1)
            value = float(minute) * 60 + float(second)
        else:
            value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value) or value < 0 or value > 120 * 60:
        return None
    if str(period) == '7' and value < 45 * 60:
        return None
    return value


def clock_label(raw: Any, period: Any) -> str:
    seconds = clock_seconds(raw, period)
    if seconds is not None:
        return '%02d:%02d' % divmod(int(seconds), 60)
    return {'31': '中场休息', '0': '等待开始', '999': '已结束'}.get(str(period), '时间未核验')


def poisson(lam: float) -> Tuple[float, ...]:
    if not math.isfinite(lam) or not 0 <= lam <= 16:
        raise ValueError('remaining goal intensity out of range')
    values = [math.exp(-lam)]
    for k in range(1, MAX_REMAINING_GOALS + 1):
        values.append(values[-1] * lam / k)
    return tuple(values)


def remaining_distribution(lam_home: float, lam_away: float,
                           score: Tuple[int, int]) -> Tuple[Tuple[int, int, float], ...]:
    home, away = poisson(lam_home), poisson(lam_away)
    # Tail is preserved as missing probability, never silently redistributed.
    return tuple((score[0] + h, score[1] + a, p * q)
                 for h, p in enumerate(home) for a, q in enumerate(away) if p * q > 1e-12)


@dataclass(frozen=True)
class Payment:
    win: float
    loss: float
    push: float
    tail: float

    @property
    def effective_probability(self) -> Optional[float]:
        total = self.win + self.loss
        return self.win / total if total > 1e-12 else None

    def ev(self, odds: float) -> float:
        return self.win * (odds - 1) - self.loss


def payment(distribution: Sequence[Tuple[int, int, float]], family: str,
            outcome: str, line: Any = '') -> Payment:
    parts = split_line(line) if family in ('OU', 'AH') else [0.0]
    if not parts or family not in ('OU', 'AH', 'HAD'):
        raise ValueError('unsupported market')
    if outcome not in ({'over', 'under'} if family == 'OU' else
                       {'home', 'away'} if family == 'AH' else {'home', 'draw', 'away'}):
        raise ValueError('unsupported outcome')
    won = lost = pushed = mass = 0.0
    for home, away, probability in distribution:
        mass += probability
        if family == 'HAD':
            actual = 'home' if home > away else 'away' if home < away else 'draw'
            if actual == outcome:
                won += probability
            else:
                lost += probability
            continue
        for part in parts:
            if family == 'OU':
                margin = (home + away - part) * (1 if outcome == 'over' else -1)
            else:
                margin = (home - away + part) * (1 if outcome == 'home' else -1)
            weight = probability / len(parts)
            if margin > 1e-8:
                won += weight
            elif margin < -1e-8:
                lost += weight
            else:
                pushed += weight
    return Payment(won, lost, pushed, max(0, 1 - mass))


def fit_total(line: Any, p_over: float, current_goals: int) -> Optional[float]:
    """Invert push-adjusted Asian totals probability; .25/.75 handled exactly."""
    if not split_line(line) or not 0 < p_over < 1:
        return None
    low, high = 0.001, 12.0
    for _ in range(FIT_ITERATIONS):
        mid = (low + high) / 2
        dist = tuple((current_goals + k, 0, p) for k, p in enumerate(poisson(mid)))
        value = payment(dist, 'OU', 'over', line).effective_probability
        if value is None or value > p_over:
            high = mid
        else:
            low = mid
    result = (low + high) / 2
    return result if 0.005 < result < 11.99 else None


def fit_share(total: float, score: Tuple[int, int], p_home: float,
              p_away: float) -> Tuple[float, float]:
    if p_home <= 0 or p_away <= 0:
        return total / 2, total / 2
    target = p_home / (p_home + p_away)
    low, high = .02, .98
    for _ in range(12):
        share = (low + high) / 2
        dist = remaining_distribution(total * share, total * (1 - share), score)
        h = payment(dist, 'HAD', 'home').win
        a = payment(dist, 'HAD', 'away').win
        value = h / (h + a) if h + a else .5
        if value > target:
            high = share
        else:
            low = share
    share = (low + high) / 2
    return total * share, total * (1 - share)


def valid_score(score: Any) -> Optional[Tuple[int, int]]:
    return _score_of(score)
