"""Offline two-sided market benchmark for already aligned half-goal totals."""

import json
import sys
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from price_contract import decimal, price_contract


def aware_time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError('timestamps must include a timezone')
    return parsed


def identity(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('missing venue or feed origin')
    return value.strip().casefold()


def price_from_verified_references(document):
    contract = document['contract']
    if (not isinstance(contract['fixture_id'], str) or not contract['fixture_id'].strip()
            or (contract['sport'], contract['phase'], contract['kind'], contract['period'],
                contract['score_basis']) != ('football', 'scheduled', 'total', 'full_time', 'full_period')):
        raise ValueError('a mapped prematch football full-time total is required')
    max_age = document['max_age_seconds']
    if type(max_age) is not int or max_age <= 0:
        raise ValueError('max_age_seconds must be a positive integer')
    cutoff = aware_time(document['as_of'])
    oldest = cutoff - timedelta(seconds=max_age)
    venues = {identity(document['offer_venue'])}
    origins = {identity(document['offer_feed_origin'])}
    references = document['references']
    if not isinstance(references, dict) or len(references) < 2:
        raise ValueError('two independent reference sources are required')

    keys = ('fixture_id', 'sport', 'phase', 'kind', 'period', 'score_basis')
    line = decimal(contract['line'])
    for quote in references.values():
        venue = identity(quote['venue'])
        origin = identity(quote['feed_origin'])
        if venue in venues or origin in origins:
            raise ValueError('offer and reference venues and feed origins must be distinct')
        venues.add(venue)
        origins.add(origin)

        reference = quote['contract']
        if any(reference[key] != contract[key] for key in keys) or decimal(reference['line']) != line:
            raise ValueError('reference contract differs from the offered contract')
        if quote['status'] != 'active':
            raise ValueError('reference quote is not active')
        quoted = aware_time(quote['quoted_at'])
        received = aware_time(quote['received_at'])
        if not oldest <= quoted <= received <= cutoff:
            raise ValueError('reference quote is stale or unavailable at the decision time')

    return price_from_references(document | {'line': line, 'direction': contract['selection']})


def price_from_references(document):
    """Price odds from a caller that has already verified source and contract alignment."""
    line = decimal(document['line'])
    if (line <= 0 or line % 1 != Decimal('0.5')
            or document['direction'] not in {'over', 'under'}):
        raise ValueError('a positive half-goal total and an over/under direction are required')
    references = document['references']
    if not isinstance(references, dict) or len(references) < 2:
        raise ValueError('two independent reference sources are required')

    scenarios = {}
    for source, quote in references.items():
        over, under = (decimal(quote[key]) for key in ('over', 'under'))
        if min(over, under) <= 1:
            raise ValueError('decimal reference odds must exceed one')
        probability_over = under / (over + under)
        # Representative scores on either side suffice for a half-goal total.
        scenarios[source] = [
            {'home': int(line) + 1, 'away': 0, 'probability': probability_over},
            {'home': int(line), 'away': 0, 'probability': 1 - probability_over},
        ]

    return price_contract(document | {
        'contract': {'sport': 'football', 'phase': 'scheduled', 'kind': 'total',
                     'period': 'full_time', 'line': line, 'selection': document['direction'],
                     'score_basis': 'full_period'},
        'scenarios': scenarios,
    })


if __name__ == '__main__':
    try:
        document = json.load(sys.stdin, parse_float=Decimal)
        print(json.dumps(price_from_verified_references(document), default=str))
    except (InvalidOperation, KeyError, OverflowError, TypeError, ValueError) as exc:
        sys.exit(f'Invalid research input: {exc}')
