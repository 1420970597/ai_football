"""Exact current-detail observations, separate from provider change timestamps.

Only the authenticated collector's single-match, no-cache detail read can supply
these witnesses. Poll receipts, connection health and model claims cannot.
Orders deliberately continue using review_state.fresh_quote and their preflight.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json

DETAIL_PATH = '/yewu11/v1/w/getMatchBaseInfoByOddsPB'
CONTEXT_FIELDS = ('phase', 'home_score', 'away_score', 'half_home_score', 'half_away_score')
QUOTE_FIELDS = ('market_id', 'market_kind', 'period', 'line', 'selection', 'price',
                'source_updated_at', 'status', 'play_id', 'market_instance_id', 'selection_id', 'score_basis')


def _stamp(value):
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if stamp.tzinfo is None:
        raise ValueError('timestamp requires timezone')
    return stamp.astimezone(timezone.utc)


def _identity(quote):
    value = {key: quote[key] for key in QUOTE_FIELDS}
    for key in ('line', 'price'):
        if value[key] is not None:
            value[key] = str(Decimal(str(value[key])).normalize())
    value['source_updated_at'] = _stamp(value['source_updated_at']).isoformat()
    return value


def confirmation_for(observation, quote, fixture, max_age, now=None):
    """Fail closed on unknown time, replay, mismatched context or changed quote."""
    now = now or datetime.now(timezone.utc)
    try:
        if not isinstance(observation, dict) or observation.get('path') != DETAIL_PATH:
            return None
        start, end = _stamp(observation['started_at']), _stamp(observation['completed_at'])
        if not start <= end <= now or not 0 <= (now - start).total_seconds() <= max_age:
            return None
        if observation['fixture_id'] != fixture['id'] or fixture['phase'] not in {'scheduled', 'live'}:
            return None
        # Normalized database source identities include the provider prefix.
        if fixture['source_id'] not in {observation['source_match_id'], 'leyu:' + observation['source_match_id']}:
            return None
        if observation['context'] != {key: fixture[key] for key in CONTEXT_FIELDS}:
            return None
        if quote['status'] != 'active' or quote['market_kind'] not in {'winner', 'handicap', 'total'}:
            return None
        if not all(quote[key] for key in ('play_id', 'market_instance_id', 'selection_id')):
            return None
        if quote['price'] is None or Decimal(str(quote['price'])) <= 1 or _stamp(quote['source_updated_at']) > end:
            return None
        identity = _identity(quote)
        candidates = observation['quotes']
        if not isinstance(candidates, list) or len(candidates) > 2000:
            return None
        matches = []
        for item in candidates:
            try:
                if _identity(item) == identity:
                    matches.append(item)
            except (KeyError, TypeError, ValueError, InvalidOperation):
                continue
        if len(matches) != 1:
            return None
        witness = {key: observation[key] for key in ('path', 'started_at', 'completed_at', 'fixture_id', 'source_match_id', 'context')}
        witness['quotes'] = [identity]
        witness['sha256'] = hashlib.sha256(json.dumps(witness, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return witness
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
        return None
