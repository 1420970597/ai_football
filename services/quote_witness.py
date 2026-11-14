"""Pure collector/API wire contract; fingerprints never authorize a signal."""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

DETAIL_PATH = '/yewu11/v1/w/getMatchBaseInfoByOddsPB'
CONTEXT_FIELDS = ('phase', 'home_score', 'away_score', 'half_home_score', 'half_away_score')
QUOTE_FIELDS = ('market_id', 'market_kind', 'period', 'line', 'selection', 'price',
                'source_updated_at', 'status', 'play_id', 'market_instance_id', 'selection_id', 'score_basis')
MAX_KNOWN_QUOTES = 128


def aware_stamp(value):
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if stamp.tzinfo is None:
        raise ValueError('timestamp requires timezone')
    return stamp.astimezone(timezone.utc)


def quote_identity(quote):
    value = {key: quote[key] for key in QUOTE_FIELDS}
    for key in ('line', 'price'):
        if value[key] is not None:
            value[key] = str(Decimal(str(value[key])).normalize())
    value['source_updated_at'] = aware_stamp(value['source_updated_at']).isoformat()
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_known_snapshot(value):
    if not isinstance(value, dict) or set(value) != {'context_sha256', 'quote_sha256'}:
        raise ValueError('invalid known quote snapshot')
    context, quotes = value['context_sha256'], value['quote_sha256']
    if (not isinstance(context, str) or not re.fullmatch(r'[0-9a-f]{64}', context)
            or not isinstance(quotes, list) or not 1 <= len(quotes) <= MAX_KNOWN_QUOTES
            or any(not isinstance(item, str) or not re.fullmatch(r'[0-9a-f]{64}', item) for item in quotes)
            or len(set(quotes)) != len(quotes)):
        raise ValueError('invalid known quote snapshot')
    return {'context_sha256': context, 'quote_sha256': sorted(quotes)}


def known_quote_snapshot(fixture, quotes):
    """Offer a cost hint only for a bounded, complete set of native identities."""
    if not quotes or len(quotes) > MAX_KNOWN_QUOTES:
        return None
    try:
        identities = []
        for quote in quotes:
            if (quote['status'] != 'active' or Decimal(str(quote['price'])) <= 1
                    or quote['market_kind'] not in {'winner', 'handicap', 'total', 'team_total'}
                    or not all(quote[key] for key in ('play_id', 'market_instance_id', 'selection_id'))):
                return None
            identity = quote_identity(quote)
            if any(value is not None and not isinstance(value, str) for value in identity.values()):
                return None
            identities.append(fingerprint(identity))
        return validate_known_snapshot({'context_sha256': fingerprint({key: fixture[key] for key in CONTEXT_FIELDS}),
                                        'quote_sha256': identities})
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
        return None


def snapshot_matches(known, observation, *, fixture_id, source_match_id):
    """Only an exact native detail may skip an identical projection upload.

    The API still checks time, active lease, binding and its post-I/O rows. This
    comparison grants no permission and never changes a provider timestamp.
    """
    try:
        known = validate_known_snapshot(known)
        if (not isinstance(observation, dict) or observation.get('path') != DETAIL_PATH
                or observation.get('fixture_id') != fixture_id
                or observation.get('source_match_id') != source_match_id
                or fingerprint(observation['context']) != known['context_sha256']
                or not isinstance(observation.get('quotes'), list) or len(observation['quotes']) > 2000):
            return False
        counts = Counter()
        for quote in observation['quotes']:
            try:
                counts[fingerprint(quote_identity(quote))] += 1
            except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
                continue  # An unrelated malformed row cannot confirm a requested identity.
        return all(counts[identity] == 1 for identity in known['quote_sha256'])
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
        return False
