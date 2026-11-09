"""Normalize explicit totals shorthand without guessing period or replacing filters."""
from decimal import Decimal
import re


_TOTAL = re.compile(r'(?:(?P<before>[大小])\s*)?(?P<a>\d{1,5}(?:\.\d{1,3})?)'
                    r'(?:\s*/\s*(?P<b>\d{1,5}(?:\.\d{1,3})?))?\s*(?P<after>[大小])?')


def market_filters(body):
    filters = {key: getattr(body, key) for key in
               ('market_kind', 'period', 'line', 'selection', 'market_query')}
    match = _TOTAL.fullmatch(filters['market_query'] or '')
    if match is None or bool(match['before']) == bool(match['after']):
        return filters
    line = Decimal(match['a'])
    if match['b'] is not None:
        other = Decimal(match['b'])
        # An Asian split total is two adjacent half-goal boundaries.
        if abs(other - line) != Decimal('0.5'):
            return filters
        line = (line + other) / 2
    if line > Decimal('99999.999') or line % Decimal('0.25'):
        return filters
    explicit = {'market_kind': 'total', 'line': line,
                'selection': 'over' if (match['before'] or match['after']) == '大' else 'under'}
    if any(filters[key] is not None and filters[key] != value for key, value in explicit.items()):
        return filters
    # The original query remains in the immutable tool parameters. Return the
    # exact effective scope; period stays unspecified unless the caller set it.
    return filters | explicit | {'market_query': None}
