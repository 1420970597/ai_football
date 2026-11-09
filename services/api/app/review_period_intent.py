"""Constrain explicit textual totals periods; never infer intent from availability.

Period-adjacent totals and their same-clause numeric alternatives share the
same period and direction. Other selections, image-only messages and unrelated
commentary do not gain an invented period. Ambiguous shorthand stays unresolved.
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
_NEXT_LINE = re.compile(
    r'[ \t]+(?P<a>\d{1,5}(?:\.\d{1,3})?)'
    r'(?:[ \t]*/[ \t]*(?P<b>\d{1,5}(?:\.\d{1,3})?))?(?=$|[ \t])')
_AMOUNT_SUFFIX = re.compile(r'(?:注|仓|倉|成|手|米|元|块|塊|[uU]\b|单位|單位|倍|水|赔率|賠率)')
_PERIODS = {'上半场': 'first_half', '上半場': 'first_half', '上半': 'first_half',
            '下半场': 'second_half', '下半場': 'second_half', '下半': 'second_half',
            '全场': 'full_time', '全場': 'full_time'}
CORRECTION_REASONS = ('review_source_period_intent_ambiguous', 'review_source_period_intent_conflict',
                      'review_source_line_intent_conflict', 'review_source_selection_intent_conflict',
                      'review_source_period_not_supported', 'review_source_selection_not_supported')
INTENT_VALID_SQL = ("NOT (s.withdrawn_at IS NOT NULL AND COALESCE(s.withdrawal_reason,'') IN ("
                    "'review_source_period_intent_ambiguous','review_source_period_intent_conflict',"
                    "'review_source_line_intent_conflict','review_source_selection_intent_conflict',"
                    "'review_source_period_not_supported','review_source_selection_not_supported'))")

_UNCERTAIN_PERIOD = re.compile(
    r'(?:未|没有|没|无|无法)\s*(?:明确|明示|标明|确认|给出|判断)?\s*(?:推荐)?期间'
    r'|(?:来源|原文|正文|原图|图片)?期间\s*(?:未知|不明|不明确|未明确|未确认|缺失)'
    r'|(?:source\s+)?period\s+(?:is\s+)?(?:unknown|unclear|unspecified|not\s+explicit)', re.I)
_INVENTORY_SUBSTITUTION = re.compile(
    r'(?:按|依据|根据|参照|基于|以)[^。；;\n]{0,24}'
    r'(?:库存|可售|可用盘口|唯一(?:同线)?选项|最近似选项)[^。；;\n]{0,24}(?:录|记|推|认|选|替|作)'
    r'|us(?:e|ed|es|ing)\s+(?:the\s+)?(?:only\s+available|inventory)', re.I)
_NEGATED_SUBSTITUTION = re.compile(r'(?:不|不能|不可|不应|不得|不会|没有|无需|不是|not|never)\s*$', re.I)


def period_intent_key(quote):
    """Canonical per-pick denial key; its binding supplies fixture identity."""
    if quote.get('market_kind') not in {'total','team_total'} or quote.get('line') is None:
        return None
    line=Decimal(str(quote['line']))
    if not line.is_finite():
        return None
    number=format((line if line else Decimal(0)).normalize(),'f')
    return ':'.join((quote['market_kind'],quote['period'],number,quote['selection']))


def evidence_period_rejection(evidence_text, quote):
    """A contradictory admission can deny a pick, never prove image contents."""
    key=period_intent_key(quote)
    if key is None:
        return None
    for denied in quote.get('prior_source_period_denials') or []:
        if isinstance(denied,dict) and denied.get(key)=='source_period_intent_ambiguous':
            return 'source_period_intent_ambiguous'
    text=unicodedata.normalize('NFKC',evidence_text or '')[:3000]
    if _UNCERTAIN_PERIOD.search(text):
        for match in _INVENTORY_SUBSTITUTION.finditer(text):
            if not _NEGATED_SUBSTITUTION.search(text[max(0,match.start()-12):match.start()]):
                return 'source_period_intent_ambiguous'
    return None


def _asian_line(first, second):
    line = Decimal(first)
    if second is not None:
        other = Decimal(second)
        if abs(other - line) != Decimal('.5'):
            return None
        line = (line + other) / 2
    return None if line % Decimal('.25') else line


def _text_totals(text):
    text = unicodedata.normalize('NFKC', text or '')
    for match in _TOTAL.finditer(text):
        line = _asian_line(match['a'] or match['c'], match['b'] or match['d'])
        if line is None:
            continue
        selection = 'over' if (match['before'] or match['after']) == '大' else 'under'
        yield match['period'], selection, line
        for line in following_total_lines(text, match.end()):
            yield match['period'], selection, line


def following_total_lines(text, start):
    """Same-clause numeric alternatives share direction; stakes do not."""
    while following := _NEXT_LINE.match(text, start):
        line = _asian_line(following['a'], following['b'])
        if line is None or _AMOUNT_SUFFIX.match(text[following.end():].lstrip(' \t')):
            break
        yield line
        start = following.end()


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
                and -60 <= (observed-posted_at).total_seconds() <= 300
                and 0 <= (now-observed).total_seconds() <= 180
                and 0 <= (now-current).total_seconds() <= 120
                and observed <= current)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return False


def period_intent_rejection(text, quote, *, posted_at=None, now=None):
    if quote.get('market_kind') not in {'total', 'team_total'} or quote.get('line') is None:
        return None
    constraints = set()
    for raw_period, selection, line in _text_totals(text):
        if line != quote['line'] or selection != quote['selection']:
            continue
        period = _PERIODS.get(raw_period, 'ambiguous')
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


def premarket_period_rejection(text, quote, declaration, *, has_visual_source=False,
                               posted_at=None, now=None):
    """An unknown pre-market period cannot be supplied by the inventory.

    A concrete textual line alone does not establish its period. An explicit
    source period for this line/direction retains the existing per-pick guards.
    The declaration is attempt/fixture-bound and write-once, not model prose
    submitted alongside an already chosen quote.
    """
    if (has_visual_source and quote.get('market_kind') in {'total','team_total'}
            and isinstance(declaration,dict)
            and declaration.get('algorithm') == 'premarket-source-intent-v1'):
        # The first read filter is a legacy single-period declaration. New
        # callers separate source periods from inventory filters, including an
        # explicit empty list for unknown and multiple periods for image picks.
        periods = declaration.get('source_periods', [declaration.get('period')])
        if isinstance(periods,list) and quote.get('period') in periods:
            return None
        for raw_period, selection, line in _text_totals(text):
            if line != quote.get('line') or selection != quote.get('selection'):
                continue
            period = _PERIODS.get(raw_period)
            if period == quote.get('period') or (period is None
                    and quote.get('period') == 'first_half'
                    and _early_first_half(quote, posted_at, now)):
                return None
        return 'source_period_unverified_before_market_read'
    return None


def verified_early_first_half_context(text, quote, *, posted_at=None, now=None):
    """Return the immutable source facts that justified a bare-half decision."""
    if quote.get('market_kind') not in {'total', 'team_total'} or quote.get('line') is None:
        return None
    for raw_period, selection, line in _text_totals(text):
        if raw_period not in {'半', '半场', '半場'}:
            continue
        if line != quote['line'] or selection != quote['selection'] or quote['period'] != 'first_half':
            continue
        if not _early_first_half(quote, posted_at, now):
            return None
        candidate = quote['intent_candidate']
        observed = datetime.fromisoformat(candidate['received_at'].replace('Z', '+00:00'))
        received = quote['fixture_received_at']
        return {'algorithm': 'leyu-early-first-half-stage-v2',
                'fixture_id': quote['fixture_id'], 'source_id': quote['source_id'],
                'message_posted_at': posted_at.isoformat(), 'source_age_limit_seconds': 300,
                'match_state': '6', 'search_clock_minute': candidate['clock_minute'],
                'quote_clock_minute': quote['fixture_clock_minute'],
                'search_received_at': observed.isoformat(),
                'quote_received_at': received.isoformat()}
    return None
