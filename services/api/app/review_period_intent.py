"""Constrain explicit textual totals periods; never infer intent from availability.

Only period-adjacent totals with the same exact Asian line and direction are
applicable. Other selections, image-only messages and unrelated commentary do
not gain an invented period. Ambiguous shorthand remains unresolved.
"""
from decimal import Decimal
from datetime import datetime, timezone
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


def _early_first_half(quote, posted_at, now):
    """Use attempt-bound provider snapshots, never a model's claimed period."""
    candidate = quote.get('intent_candidate')
    if not isinstance(candidate, dict) or posted_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        observed = datetime.fromisoformat(candidate['received_at'].replace('Z', '+00:00'))
        current = quote['fixture_received_at']
        if (candidate.get('id') != quote['fixture_id']
                or candidate.get('source_id') != quote.get('source_id')
                or not re.fullmatch(r'leyu:[0-9]+', quote['fixture_id'])
                or candidate.get('sport') != 'football'
                or quote.get('fixture_sport') != 'football'
                or candidate.get('phase') != 'live' or quote.get('phase') != 'live'
                or candidate.get('match_state') != '6'
                or quote.get('fixture_match_state') != '6'
                or type(candidate.get('clock_minute')) is not int
                or type(quote.get('fixture_clock_minute')) is not int
                or not 0 <= candidate['clock_minute'] < 45
                or not 0 <= quote['fixture_clock_minute'] < 45):
            return False
        # Receipt times describe observations, not invented provider timestamps.
        return (0 <= (now-posted_at).total_seconds() <= 300
                and -60 <= (observed-posted_at).total_seconds() <= 120
                and 0 <= (now-observed).total_seconds() <= 180
                and 0 <= (now-current).total_seconds() <= 120
                and observed <= current)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return False


def period_intent_rejection(text, quote, *, posted_at=None, now=None):
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
        period = _PERIODS.get(match['period'], 'ambiguous')
        if period == 'ambiguous' and _early_first_half(quote, posted_at, now):
            period = 'first_half'
        constraints.add(period)
    if not constraints:
        return None
    if 'ambiguous' in constraints:
        return 'source_period_intent_ambiguous'
    if quote['period'] not in constraints:
        return 'source_period_intent_conflict'
    return None


def verified_early_first_half_context(text, quote, *, posted_at=None, now=None):
    """Return the immutable source facts that justified a bare-half decision."""
    if quote.get('market_kind') not in {'total', 'team_total'} or quote.get('line') is None:
        return None
    text = unicodedata.normalize('NFKC', text or '')
    for match in _TOTAL.finditer(text):
        if match['period'] not in {'半', '半场', '半場'}:
            continue
        selection = 'over' if (match['before'] or match['after']) == '大' else 'under'
        line = Decimal(match['a'] or match['c'])
        split = match['b'] or match['d']
        if split is not None:
            other = Decimal(split)
            if abs(other-line) != Decimal('.5'):
                continue
            line = (line+other)/2
        if line != quote['line'] or selection != quote['selection'] or quote['period'] != 'first_half':
            continue
        if not _early_first_half(quote, posted_at, now):
            return None
        candidate = quote['intent_candidate']
        observed = datetime.fromisoformat(candidate['received_at'].replace('Z', '+00:00'))
        received = quote['fixture_received_at']
        return {'algorithm': 'leyu-early-first-half-stage-v1',
                'fixture_id': quote['fixture_id'], 'source_id': quote['source_id'],
                'match_state': '6', 'search_clock_minute': candidate['clock_minute'],
                'quote_clock_minute': quote['fixture_clock_minute'],
                'search_received_at': observed.isoformat(),
                'quote_received_at': received.isoformat()}
    return None
