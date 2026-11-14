"""Normalize explicit totals shorthand without guessing period or replacing filters."""
from decimal import Decimal
import re

from .review_line_intent import evidence_line_intent


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
    explicit = {'market_kind': 'team_total' if filters['market_kind'] == 'team_total' else 'total', 'line': line,
                'selection': 'over' if (match['before'] or match['after']) == '大' else 'under'}
    if any(filters[key] is not None and filters[key] != value for key, value in explicit.items()):
        return filters
    # The original query remains in the immutable tool parameters. Return the
    # exact effective scope; period stays unspecified unless the caller set it.
    return filters | explicit | {'market_query': None}


def handicap_query_guidance(body):
    """Do not present an unsupported signed source query as absent inventory."""
    if (body.market_kind != 'handicap' or not body.market_query
            or not evidence_line_intent(body.market_query, 'handicap')):
        return None
    return dict(reason='handicap_market_query_requires_structured_filter',
        retry_without_market_query=True,
        message='market_query的带符号让球表达不能按玩法名称过滤，不表示库存没有该盘口。'
                '保留已绑定fixture、原来源期间及market_kind=handicap；省略market_query及口径未确认的line，'
                '读取玩法期间整组，再逐行按selection与selection_line核对原文，不从原生line猜主客方向。')
