"""Offline quote benchmark for independently verified, identical market contracts."""

import json
import sys
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_FLOOR

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


def adjacent_lines(kind, line):
    if line is None or abs(line) % 1 == Decimal('0.5'):
        raise ValueError('adjacent half lines require an integer or quarter line')
    # Higher totals reduce Over coverage. A lower home handicap reduces Home coverage.
    factor = -1 if kind == 'handicap' else 1
    threshold = line * factor
    half = Decimal('0.5')
    center = (threshold + half).to_integral_value(rounding=ROUND_FLOOR)
    return ((center - half) * factor, (center + half) * factor), threshold - center


def book_probability(quote, sides):
    limits = []
    for side in sides:
        book = quote['order_book'][side]
        bid, ask = decimal(book['bid']), decimal(book['ask'])
        if not 0 < bid <= ask < 1 or min(decimal(book['bid_size']), decimal(book['ask_size'])) <= 0:
            raise ValueError('reference needs valid probability quotes and positive depth on both sides')
        limits.append((bid, ask))
    first, second = limits
    lower, upper = max(first[0], 1 - second[1]), min(first[1], 1 - second[0])
    if lower > upper:
        raise ValueError('opposite sides imply inconsistent probability ranges')
    return lower, upper


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
    reference_mode = document.get('reference_mode', 'same_line')
    if reference_mode not in {'same_line', 'adjacent_half_lines'}:
        raise ValueError('unknown reference mode')
    required_lines = (line,)
    if reference_mode == 'adjacent_half_lines':
        required_lines, offset = adjacent_lines(contract['kind'], line)
        max_skew = document['max_quote_skew_seconds']
        if type(max_skew) is not int or not 0 <= max_skew <= max_age:
            raise ValueError('max_quote_skew_seconds must be within the maximum quote age')

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
    book_ranges = {}
    for source, quote in references.items():
        venue = identity(quote['venue'])
        origin = identity(quote['feed_origin'])
        if venue in venues or origin in origins:
            raise ValueError('offer and reference venues and feed origins must be distinct')
        venues.add(venue)
        origins.add(origin)

        samples = quote['adjacent_quotes'] if len(required_lines) == 2 else [quote]
        if not isinstance(samples, list) or len(samples) != len(required_lines):
            raise ValueError('each source must include both adjacent half lines')
        verified = {}
        for sample in samples:
            reference = sample['contract']
            if any(reference.get(key) != contract.get(key) for key in keys):
                raise ValueError('reference contract differs from the offered contract')
            ref_line = market_terms(reference)[1]
            if ref_line not in required_lines or ref_line in verified:
                raise ValueError('reference contract differs from the required lines')
            if sample['status'] != 'active':
                raise ValueError('reference quote is not active')
            quoted_times.append(quote_time(sample['quoted_at'], sample['received_at'], oldest, cutoff))
            if state is not None:
                reference_state, confirmed = live_state(sample['event_state'], contract['period'], cutoff)
                if reference_state != state:
                    raise ValueError('reference live state differs from the offered state')
                latest_confirmation = max(latest_confirmation, confirmed)
            verified[ref_line] = sample
        if len(required_lines) == 2:
            book_ranges[source] = tuple(book_probability(verified[required], sides) for required in required_lines)

    if state is not None and min(quoted_times) < latest_confirmation:
        raise ValueError('quote predates the latest confirmed live state')
    if state is not None and contract['kind'] in {'total', 'team_total'}:
        home, away = state[1:3]
        score = home + away if contract['kind'] == 'total' else {'home': home, 'away': away}[contract['team_side']]
        if score > line:
            raise ValueError('the live total already exceeds the line')
        if reference_mode == 'adjacent_half_lines' and score > min(required_lines):
            raise ValueError('the live total already exceeds an adjacent reference line')
    if reference_mode == 'adjacent_half_lines':
        if max(quoted_times) - min(quoted_times) > timedelta(seconds=max_skew):
            raise ValueError('quote timestamps exceed the allowed skew')
        return price_adjacent_market(document, book_ranges, sides, offset)
    return price_reference_market(document, sides, contract['selection'], line)


def price_adjacent_market(document, book_ranges, sides, offset):
    selection = document['contract']['selection']
    cost, margin, offer = pricing_terms(document, sides, selection)
    scenarios = {}
    for source, (looser, tighter) in book_ranges.items():
        if looser[1] < tighter[0]:
            raise ValueError('adjacent lines imply inconsistent scoring probabilities')
        # Worst compatible pair for the selected side, under P(X>k-0.5) >= P(X>k+0.5).
        if selection == sides[0]:
            low, high = max(looser[0], tighter[0]), tighter[0]
        else:
            low, high = looser[1], min(tighter[1], looser[1])
        half_center = (low - high) / 2
        win, loss = high, 1 - low
        if offset < 0:
            win += half_center
        elif offset > 0:
            loss += half_center
        if selection != sides[0]:
            win, loss = loss, win
        max_push = (looser[1] - tighter[0]) / (2 if offset else 1)
        scenarios[source] = {
            'expected_win_fraction': win,
            'expected_loss_fraction': loss,
            'net_at_offered_odds': win * (offer - 1) - loss - cost,
            'minimum_odds': 1 + (loss + cost + margin) / win if win else None,
            'active_stake_lower_bound': 1 - max_push,
        }
    prices = [scenario['minimum_odds'] for scenario in scenarios.values()]
    minimum = max(prices) if all(price is not None for price in prices) else None
    worst_net = min(scenario['net_at_offered_odds'] for scenario in scenarios.values())
    return {'reference_mode': 'adjacent_half_lines', 'minimum_odds': minimum,
            'worst_net_at_offered_odds': worst_net, 'passes_price_test': minimum is not None and worst_net >= margin,
            'active_stake_lower_bound': min(scenario['active_stake_lower_bound'] for scenario in scenarios.values()),
            'scenarios': scenarios}


def price_from_references(document):
    """Price odds from a caller that has already verified source and contract alignment."""
    line = decimal(document['line'])
    if line <= 0 or line % 1 != Decimal('0.5'):
        raise ValueError('a positive half-goal total is required')
    return price_reference_market(document, MARKET_SIDES['total'], document['direction'], line)


def pricing_terms(document, sides, selection):
    cost = decimal(document['cost_per_unit'])
    margin = decimal(document['required_net_edge'])
    offer = decimal(document['offered_odds'])
    if selection not in sides or cost < 0 or margin < 0 or offer <= 1:
        raise ValueError('invalid market side, cost, edge or odds')
    return cost, margin, offer


def price_reference_market(document, sides, selection, line):
    references = document['references']
    if not isinstance(references, dict) or len(references) < 2:
        raise ValueError('two independent reference sources are required')
    cost, margin, offer = pricing_terms(document, sides, selection)

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
