"""Exact half-line neighbors for integer and quarter-line contracts."""

from decimal import Decimal, ROUND_FLOOR


def adjacent_lines(kind, line):
    if line is None or not line.is_finite() or abs(line) % 1 == Decimal('0.5'):
        raise ValueError('adjacent half lines require an integer or quarter line')
    # Higher totals and smaller home handicaps both reduce the winning event.
    factor = -1 if kind == 'handicap' else 1
    threshold = line * factor
    half = Decimal('0.5')
    center = (threshold + half).to_integral_value(rounding=ROUND_FLOOR)
    return ((center - half) * factor, (center + half) * factor), threshold - center
