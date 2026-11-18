"""Offline quote benchmark for independently verified, identical market contracts."""

import json
import sys
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from price_contract import decimal


MARKET_SIDES = {'total': ('over', 'under'), 'team_total': ('over', 'under'),
                'handicap': ('home', 'away'), 'winner': ('home', 'draw', 'away')}


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


def market_terms(contract):
    sport, kind, period, phase = (contract[key] for key in ('sport', 'kind', 'period', 'phase'))
    basis = contract['score_basis']
    if (sport not in {'football', 'basketball'} or kind not in MARKET_SIDES
            or phase not in {'scheduled', 'live'} or period not in {'full_time', 'first_half'}
            or basis != 'full_period' and (sport, kind, period, phase, basis) != (
                'football', 'handicap', 'full_time', 'live', 'remaining_period')):
        raise ValueError('unsupported or ambiguous market contract')
    sides = MARKET_SIDES[kind]
    if sport == 'basketball':
        if (phase != 'scheduled' or period != 'full_time' or kind == 'team_total'
                or contract.get('settlement_scope') not in {'regulation', 'including_overtime'}
                or kind == 'winner' and contract['settlement_scope'] != 'including_overtime'):
            raise ValueError('basketball market requires compatible full-time settlement scope')
        if kind == 'winner':
            sides = ('home', 'away')
    elif contract.get('settlement_scope') not in {None, 'regulation'}:
        raise ValueError('football market requires regulation settlement scope')
    if kind == 'handicap' and contract.get('line_basis') != 'provider_home':
        raise ValueError('handicap requires a home-team line basis')
    if kind == 'team_total' and contract.get('team_side') not in {'home', 'away'}:
        raise ValueError('team total requires the team side')

    raw_line = contract.get('line')
    line = None if raw_line is None else decimal(raw_line)
    if kind == 'winner':
        if line not in {None, 0}:
            raise ValueError('winner market has no scoring line')
        return sides, None
    step = Decimal('0.25') if sport == 'football' else Decimal('0.5')
    if line is None or line % step or kind in {'total', 'team_total'} and line <= 0:
        raise ValueError('invalid scoring line')
    return sides, line


def price_from_verified_references(document):
    contract = document['contract']
    identity(contract['fixture_id'])
    sides, line = market_terms(contract)
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

    keys = ('fixture_id', 'sport', 'phase', 'kind', 'period', 'score_basis',
            'line_basis', 'team_side', 'settlement_scope')
    state = None
    quoted_times = []
    offer_keys = ('offer_status', 'offer_quoted_at', 'offer_received_at')
    legacy_scheduled_total = (
        (contract['phase'], contract['sport'], contract['kind']) == ('scheduled', 'football', 'total')
        and line % 1 == Decimal('0.5')
    )
    if not legacy_scheduled_total or any(key in document for key in offer_keys):
        if document['offer_status'] != 'active':
            raise ValueError('offered quote is not active')
        quoted_times.append(quote_time(document['offer_quoted_at'], document['offer_received_at'], oldest, cutoff))
    if contract['phase'] == 'live':
        state, latest_confirmation = live_state(document['event_state'], contract['period'], cutoff)
    for quote in references.values():
        venue = identity(quote['venue'])
        origin = identity(quote['feed_origin'])
        if venue in venues or origin in origins:
            raise ValueError('offer and reference venues and feed origins must be distinct')
        venues.add(venue)
        origins.add(origin)

        reference = quote['contract']
        if any(reference.get(key) != contract.get(key) for key in keys) or market_terms(reference)[1] != line:
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
    if state is not None and contract['kind'] in {'total', 'team_total'}:
        home, away = state[1:3]
        score = home + away if contract['kind'] == 'total' else {'home': home, 'away': away}[contract['team_side']]
        if score > line:
            raise ValueError('the live total already exceeds the line')
    return price_reference_market(document, sides, contract['selection'], line)


def price_from_references(document):
    """Price odds from a caller that has already verified source and contract alignment."""
    line = decimal(document['line'])
    if line <= 0 or line % 1 != Decimal('0.5'):
        raise ValueError('a positive half-goal total is required')
    return price_reference_market(document, MARKET_SIDES['total'], document['direction'], line)


def price_reference_market(document, sides, selection, line):
    references = document['references']
    if not isinstance(references, dict) or len(references) < 2:
        raise ValueError('two independent reference sources are required')
    cost = decimal(document['cost_per_unit'])
    margin = decimal(document['required_net_edge'])
    offer = decimal(document['offered_odds'])
    if selection not in sides or cost < 0 or margin < 0 or offer <= 1:
        raise ValueError('invalid market side, cost, edge or odds')

    if line is None or abs(line) % 1 == Decimal('0.5'):
        active_floor = Decimal(1)
    elif line % 1:
        active_floor = Decimal('0.5')
    else:
        active_floor = decimal(document.get('active_stake_lower_bound', 0))
        if not 0 <= active_floor <= 1:
            raise ValueError('active stake lower bound must be between zero and one')

    scenarios = {}
    for source, quote in references.items():
        prices = {side: decimal(quote[side]) for side in sides}
        if min(prices.values()) <= 1:
            raise ValueError('decimal reference odds must exceed one')
        # Proportional de-vig estimates the win share of active stake, including split lines.
        if len(sides) == 2:
            opposite, = (price for side, price in prices.items() if side != selection)
            win_share = opposite / (prices[selection] + opposite)
        else:
            win_share = 1 / sum(prices[selection] / price for price in prices.values())
        action_edge = win_share * offer - 1
        # A negative edge is worst when all stake is active.
        active = active_floor if action_edge >= 0 else Decimal(1)
        scenarios[source] = {
            'conditional_fair_odds': 1 / win_share,
            'expected_win_fraction': win_share * active,
            'expected_loss_fraction': (1 - win_share) * active,
            'net_at_offered_odds': action_edge * active - cost,
            'minimum_odds': (1 + (cost + margin) / active_floor) / win_share if active_floor else None,
        }

    worst_net = min(scenario['net_at_offered_odds'] for scenario in scenarios.values())
    return {
        'minimum_odds': max(scenario['minimum_odds'] for scenario in scenarios.values()) if active_floor else None,
        'worst_net_at_offered_odds': worst_net,
        'passes_price_test': bool(active_floor) and worst_net >= margin,
        'active_stake_lower_bound': active_floor,
        'scenarios': scenarios,
    }


if __name__ == '__main__':
    try:
        document = json.load(sys.stdin, parse_float=Decimal)
        print(json.dumps(price_from_verified_references(document), default=str))
    except (InvalidOperation, KeyError, OverflowError, TypeError, ValueError) as exc:
        sys.exit(f'Invalid research input: {exc}')
