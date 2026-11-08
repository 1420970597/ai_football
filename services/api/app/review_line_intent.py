"""Reject a unique explicit per-pick source line that contradicts its quote.

This is a consistency guard for already extracted evidence, not image OCR.
An absent or ambiguous line never invents a source intent. Odds are excluded.
"""
from decimal import Decimal
import re
import unicodedata

from .handicap_lines import line_terms
from .review_period_intent import following_total_lines

_NUMBER = r'(?:\d{1,3}(?:\.\d{1,3})?|\.\d{1,3})'
_LINE = rf'(?P<a>{_NUMBER})(?:\s*/\s*(?P<second_sign>[+-])?\s*(?P<b>{_NUMBER}))?'
_HANDICAP = re.compile(rf'(?:(?<![A-Za-z0-9_./@])(?P<sign>[+-])|(?P<chinese>受让|受讓|让|讓))\s*{_LINE}'
                       r'(?![\d./+—–-])(?!(?:[ \t]*)(?:[uU]\b|元|注|倍|水|仓|倉|单位|單位|:))')
_TOTAL = re.compile(rf'(?<![大小])(?P<direction>[大小])\s*{_LINE}(?![\d./+—–-])')
_PRICE_PREFIX = re.compile(r'(?:赔率|賠率|倍率|odds|@)\s*$', re.IGNORECASE)


def _line(match, *, negative=False):
    first = Decimal(match['a'])
    if negative:
        first = -first
    if match['b'] is not None:
        sign = match['second_sign']
        second = Decimal(match['b'])
        if sign == '-' or sign is None and negative:
            second = -second
        if abs(first-second) != Decimal('.5'):
            return None
        first = (first+second)/2
    return first if first % Decimal('.25') == 0 else None


def line_intent_rejection(evidence_text, quote):
    kind = quote.get('market_kind')
    if kind not in {'handicap', 'total', 'team_total'} or quote.get('line') is None:
        return None
    text = unicodedata.normalize('NFKC', evidence_text or '').replace('−', '-')
    if kind == 'handicap':
        lines = {_line(m, negative=(m['sign'] or m['chinese']) in {'-', '让', '讓'})
                 for m in _HANDICAP.finditer(text)
                 if not _PRICE_PREFIX.search(text[max(0,m.start()-12):m.start()])} - {None}
        if len(lines) != 1:
            return None
        selected_line = line_terms(quote)['selection_line']
        return ('source_line_intent_conflict' if Decimal(selected_line) != next(iter(lines)) else None)
    intents = set()
    for match in _TOTAL.finditer(text):
        direction = 'over' if match['direction'] == '大' else 'under'
        line = _line(match)
        if line is not None:
            intents.add((direction, line))
            intents.update((direction, alternative) for alternative in following_total_lines(text, match.end()))
    directions = {direction for direction, _ in intents}
    if len(directions) != 1:
        return None
    direction = next(iter(directions))
    if quote.get('selection') != direction:
        return 'source_selection_intent_conflict'
    return ('source_line_intent_conflict'
            if (direction, Decimal(str(quote['line']))) not in intents else None)
