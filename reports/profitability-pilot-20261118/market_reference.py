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


def live_state(state, contract_period, cutoff):
    period = state['period']
    if (state['status'] != 'playing' or period not in {'first_half', 'second_half'}
            or contract_period == 'first_half' and period != 'first_half'):
        raise ValueError('the live period must be playing and confirmed')
    values = tuple(state[key] for key in ('home_score', 'away_score', 'home_red_cards', 'away_red_cards'))
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError('live scores and red card counts must be nonnegative integers')
    confirmed = aware_time(state['confirmed_at'])
    received = aware_time(state['received_at'])
    if not confirmed <= received <= cutoff:
        raise ValueError('live state was unavailable at the decision time')
    return (period, *values), confirmed


def quote_time(quoted_at, received_at, oldest, cutoff):
    quoted = aware_time(quoted_at)
    received = aware_time(received_at)
    if not oldest <= quoted <= received <= cutoff:
        raise ValueError('reference quote is stale or unavailable at the decision time')
    return quoted


def price_from_verified_references(document):
    contract = document['contract']
    if (not isinstance(contract['fixture_id'], str) or not contract['fixture_id'].strip()
            or (contract['sport'], contract['kind'], contract['score_basis']) != ('football', 'total', 'full_period')
            or contract['phase'] not in {'scheduled', 'live'}
            or contract['period'] not in {'full_time', 'first_half'}):
        raise ValueError('a mapped football full-period total is required')
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
    state = None
    quoted_times = []
    if contract['phase'] == 'live':
        if document['offer_status'] != 'active':
            raise ValueError('offered quote is not active')
        state, latest_confirmation = live_state(document['event_state'], contract['period'], cutoff)
        quoted_times.append(quote_time(document['offer_quoted_at'], document['offer_received_at'], oldest, cutoff))
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
        quoted = quote_time(quote['quoted_at'], quote['received_at'], oldest, cutoff)
        if state is not None:
            reference_state, confirmed = live_state(quote['event_state'], contract['period'], cutoff)
            if reference_state != state:
                raise ValueError('reference live state differs from the offered state')
            latest_confirmation = max(latest_confirmation, confirmed)
            quoted_times.append(quoted)

    if state is not None and min(quoted_times) < latest_confirmation:
        raise ValueError('quote predates the latest confirmed live state')
    score = state[1:3] if state is not None else (0, 0)
    return price_half_goal_totals(document, contract | {'line': line}, score)


def price_from_references(document):
    """Price odds from a caller that has already verified source and contract alignment."""
    contract = {'sport': 'football', 'phase': 'scheduled', 'kind': 'total',
                'period': 'full_time', 'line': decimal(document['line']),
                'selection': document['direction'], 'score_basis': 'full_period'}
    return price_half_goal_totals(document, contract, (0, 0))


def price_half_goal_totals(document, contract, score):
    line = contract['line']
    if (line <= 0 or line % 1 != Decimal('0.5')
            or contract['selection'] not in {'over', 'under'}):
        raise ValueError('a positive half-goal total and an over/under direction are required')
    if line < sum(score):
        raise ValueError('the live total already exceeds the line')
    references = document['references']
    if not isinstance(references, dict) or len(references) < 2:
        raise ValueError('two independent reference sources are required')

    scenarios = {}
    for source, quote in references.items():
        over, under = (decimal(quote[key]) for key in ('over', 'under'))
        if min(over, under) <= 1:
            raise ValueError('decimal reference odds must exceed one')
        probability_over = under / (over + under)
        scenarios[source] = []
        # Both representative outcomes preserve the known live score.
        for total, probability in ((int(line) + 1, probability_over), (int(line), 1 - probability_over)):
            home, away = total - score[1], score[1]
            result = {'home': home, 'away': away, 'probability': probability}
            if contract['period'] == 'first_half':
                result.update(half_home=home, half_away=away)
            scenarios[source].append(result)

    return price_contract(document | {'contract': contract, 'scenarios': scenarios})


if __name__ == '__main__':
    try:
        document = json.load(sys.stdin, parse_float=Decimal)
        print(json.dumps(price_from_verified_references(document), default=str))
    except (InvalidOperation, KeyError, OverflowError, TypeError, ValueError) as exc:
        sys.exit(f'Invalid research input: {exc}')
