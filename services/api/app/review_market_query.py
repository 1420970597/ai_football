"""Normalize explicit totals shorthand without guessing period or replacing filters."""
from decimal import Decimal
import re


_TOTAL = re.compile(r'([大小])\s*(\d{1,5}(?:\.\d{1,3})?)(?:\s*/\s*(\d{1,5}(?:\.\d{1,3})?))?')


def market_filters(body):
    filters = {key: getattr(body, key) for key in
               ('market_kind', 'period', 'line', 'selection', 'market_query')}
    match = _TOTAL.fullmatch(filters['market_query'] or '')
    if match is None:
        return filters
    line = Decimal(match[2])
    if match[3] is not None:
        other = Decimal(match[3])
        # An Asian split total is two adjacent half-goal boundaries.
        if abs(other - line) != Decimal('0.5'):
            return filters
        line = (line + other) / 2
    if line > Decimal('99999.999') or line % Decimal('0.25'):
        return filters
    explicit = {'market_kind': 'total', 'line': line,
                'selection': 'over' if match[1] == '大' else 'under'}
    if any(filters[key] is not None and filters[key] != value for key, value in explicit.items()):
        return filters
    # The original query remains in the immutable tool parameters. Return the
    # exact effective scope; period stays unspecified unless the caller set it.
    return filters | explicit | {'market_query': None}
