"""Exact half-line neighbors for integer and quarter-line contracts."""

from decimal import Decimal, ROUND_FLOOR
import re


def full_match_line(preflight):
    """Translate the verified Leyu remaining-period handicap to the final score."""
    if (preflight.get('sport'), preflight.get('kind'), preflight.get('period'),
            preflight.get('phase'), preflight.get('market_id'), preflight.get('effective_score_basis'),
            preflight.get('line_basis'), preflight.get('settlement_rule')) != (
                'football', 'handicap', 'full_time', 'live', '4', 'remaining_period', 'provider_home',
                'leyu-football-fulltime-live-handicap-e448-20261106'):
        return None
    if re.fullmatch(r'leyu:[0-9]+', preflight.get('fixture_id', '')) is None:
        return None
    score = preflight.get('score_baseline') or {}
    home, away = score.get('home'), score.get('away')
    if preflight.get('line') is None or any(type(value) is not int or value < 0 for value in (home, away)):
        return None
    line = Decimal(str(preflight['line']))
    if not line.is_finite() or line % Decimal('0.25'):
        return None
    return line - (home - away)


def adjacent_lines(kind, line):
    if line is None or not line.is_finite() or abs(line) % 1 == Decimal('0.5'):
        raise ValueError('adjacent half lines require an integer or quarter line')
    # Higher totals and smaller home handicaps both reduce the winning event.
    factor = -1 if kind == 'handicap' else 1
    threshold = line * factor
    half = Decimal('0.5')
    center = (threshold + half).to_integral_value(rounding=ROUND_FLOOR)
    return ((center - half) * factor, (center + half) * factor), threshold - center
