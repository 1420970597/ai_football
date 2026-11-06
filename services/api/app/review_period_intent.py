"""Constrain explicit textual totals periods; never infer intent from availability.

Only period-adjacent totals with the same exact Asian line and direction are
applicable. Other selections, image-only messages and unrelated commentary do
not gain an invented period. Ambiguous shorthand remains unresolved.
"""
from decimal import Decimal
import re
import unicodedata


_TOTAL = re.compile(
    r'(?P<period>上半场|上半場|下半场|下半場|全场|全場|上半|下半|半场|半場|半)'
    r'\s*(?:(?P<before>[大小])\s*(?P<a>\d{1,5}(?:\.\d{1,3})?)'
    r'(?:\s*/\s*(?P<b>\d{1,5}(?:\.\d{1,3})?))?'
    r'|(?P<c>\d{1,5}(?:\.\d{1,3})?)'
    r'(?:\s*/\s*(?P<d>\d{1,5}(?:\.\d{1,3})?))?\s*(?P<after>[大小]))')
_PERIODS = {'上半场': 'first_half', '上半場': 'first_half', '上半': 'first_half',
            '下半场': 'second_half', '下半場': 'second_half', '下半': 'second_half',
            '全场': 'full_time', '全場': 'full_time'}
CORRECTION_REASONS = ('review_source_period_intent_ambiguous', 'review_source_period_intent_conflict')
INTENT_VALID_SQL = ("NOT (s.withdrawn_at IS NOT NULL AND COALESCE(s.withdrawal_reason,'') IN ("
                    "'review_source_period_intent_ambiguous','review_source_period_intent_conflict'))")


def period_intent_rejection(text, quote):
    if quote.get('market_kind') not in {'total', 'team_total'} or quote.get('line') is None:
        return None
    constraints = set()
    text = unicodedata.normalize('NFKC', text or '')
    for match in _TOTAL.finditer(text):
        selection = 'over' if (match['before'] or match['after']) == '大' else 'under'
        line = Decimal(match['a'] or match['c'])
        split = match['b'] or match['d']
        if split is not None:
            other = Decimal(split)
            if abs(other - line) != Decimal('.5'):
                continue
            line = (line + other) / 2
        if line % Decimal('.25') or line != quote['line'] or selection != quote['selection']:
            continue
        constraints.add(_PERIODS.get(match['period'], 'ambiguous'))
    if not constraints:
        return None
    if 'ambiguous' in constraints:
        return 'source_period_intent_ambiguous'
    if quote['period'] not in constraints:
        return 'source_period_intent_conflict'
    return None
