"""Measure quote coverage at genuine preflight cutoffs from private archives."""

import argparse
from bisect import bisect_left
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re

from preflight_price_decision import compare_with_receipts, evaluate
from reference_lines import adjacent_lines, full_match_line
from reference_stream_replay import compare_rest, read_segment, stream_quote

REFERENCE_SIDES = {'winner': {'home', 'draw', 'away'}, 'handicap': {'home', 'away'},
                   'total': {'over', 'under'}, 'team_total': {'over', 'under'}}


def instant(value):
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError('timestamp must include a timezone')
    return result


def contract_key(row):
    line = row.get('line')
    kind = row['kind']
    side = row.get('team_side') if kind == 'team_total' else None
    return row['fixture_id'], kind, row['period'], side, Decimal(str(line)) if line is not None else None


def settlement_comparison(preflight):
    if preflight.get('sport') != 'football':
        return 'sport_unverified'
    kind = preflight['kind']
    if ((kind == 'handicap' and preflight.get('line_basis') != 'provider_home')
            or (kind == 'team_total' and preflight.get('team_side') not in {'home', 'away'})):
        return 'line_orientation_unverified'
    basis = preflight.get('effective_score_basis')
    if basis == 'remaining_period':
        return ('remaining_period_reference_missing' if full_match_line(preflight) is None
                else 'equivalent_full_match_line_requires_live_state')
    if basis != 'full_period':
        return 'score_basis_unverified'
    if preflight.get('phase') == 'live':
        return 'independent_live_state_missing'
    if preflight.get('phase') == 'scheduled':
        return 'scheduled_rule_review_required'
    return 'phase_unverified'


def red_card_status(sample, received):
    observed = sample.get('red_cards', {})
    sides = ('home', 'away')
    if not all(type(observed.get(side)) is int and observed[side] >= 0 for side in sides):
        return 'missing_team_statistics'
    teams = sample.get('team_ids', {})
    if (sample.get('wallclock_available') is not True or sample.get('key_events_available') is not True
            or not all(isinstance(teams.get(side), str) and teams[side] for side in sides)
            or teams['home'] == teams['away']):
        return 'missing_event_evidence'

    team_sides = {team: side for side, team in teams.items()}
    events = sample['key_events']
    cards = [event for event in events if (event.get('type') or {}).get('type') == 'red-card']
    counts = dict.fromkeys(sides, 0)
    seen = set()
    for card in cards:
        native = card.get('wallclock')
        event_id = card.get('id')
        side = team_sides.get(card.get('team_id'))
        if not native or not event_id or event_id in seen or side is None or instant(native) > received:
            return 'invalid_red_card_event'
        seen.add(event_id)
        counts[side] += 1

    return 'source_counts_match' if observed == counts else 'source_counts_conflict'


def soccer_event_context(sample, received):
    context = {'red_card_status': red_card_status(sample, received)}
    if 'red_cards' in sample:
        context['red_cards'] = sample['red_cards']
    material = []
    for event in sample['key_events']:
        kind = (event.get('type') or {}).get('type')
        if (event.get('scoringPlay') is True or kind in {
                'goal', 'penalty---scored', 'red-card', 'kickoff', 'start-2nd-half',
                'halftime', 'end-regular-time', 'start-delay', 'end-delay'}):
            if not event.get('wallclock'):
                return context | {'latest_material_event_status': 'missing_native_event_time'}
            material.append((instant(event['wallclock']), kind))
    if material:
        latest = max(stamp for stamp, _ in material)
        context['latest_material_event_at'] = latest.isoformat()
        context['latest_material_events'] = sorted({kind for stamp, kind in material if stamp == latest and kind})
    delays = [(stamp, kind) for stamp, kind in material if kind in {'start-delay', 'end-delay'}]
    if delays:
        latest_delay = max(stamp for stamp, _ in delays)
        context['delay_status'] = (
            'delay_unresolved' if (latest_delay, 'start-delay') in delays else 'delay_ended'
        )
    return context


def match_scores(preflight, binding, records, cutoff, max_age_seconds):
    if not binding:
        return {'status': 'fixture_not_bound'}
    sample = next((row for received, row in records if received <= cutoff and row['event'] == binding['event']), None)
    if sample is None:
        return {'status': 'no_score_observation_before_preflight'}
    received = instant(sample['received_at'])
    result = {'event': sample['event'], 'received_at': sample['received_at'],
              'age_seconds': (cutoff - received).total_seconds()}
    if sample.get('error'):
        return result | {'status': 'score_source_read_failed'}
    if instant(sample['requested_at']) > received:
        return result | {'status': 'invalid_score_observation_time'}
    teams = sample['teams']
    if (sample['league'] != binding['league']
            or instant(sample['scheduled_start']) != instant(binding['scheduled_start'])
            or any(teams[side][0] != binding[side] for side in ('home', 'away'))):
        return result | {'status': 'score_fixture_mismatch'}
    if result['age_seconds'] > max_age_seconds:
        return result | {'status': 'stale_score_observation'}
    status = sample['status']
    phase = status.get('type', {})
    name, period = phase.get('name'), status.get('period')
    result |= {'period': period, 'display_clock': status.get('displayClock'), 'native_status': name}
    if 'kalshi_milestone' in binding:
        result['kalshi_comparison'] = compare_kalshi(sample, binding['kalshi_milestone'], cutoff, max_age_seconds)
    if phase.get('state') != 'in':
        return result | {'status': 'score_phase_mismatch'}
    if name == 'STATUS_HALFTIME':
        return result | {'status': 'score_play_paused'}
    expected = {1: 'STATUS_FIRST_HALF', 2: 'STATUS_SECOND_HALF'}.get(period) if type(period) is int else None
    if expected is None or name not in {expected, 'STATUS_IN_PROGRESS'}:
        return result | {'status': 'score_play_state_unverified'}
    native_times = [instant(event['wallclock']) for event in sample['key_events'] if event.get('wallclock')]
    if any(stamp > received for stamp in native_times):
        return result | {'status': 'invalid_score_event_time'}
    baseline = preflight.get('score_baseline') or {}
    observed = [teams[side][1] for side in ('home', 'away')]
    wanted = [baseline.get(side) for side in ('home', 'away')]
    result |= {'latest_native_event_at': max(native_times).isoformat() if native_times else None,
               'event_context': soccer_event_context(sample, received)}
    if not all(type(value) is int and value >= 0 for value in wanted):
        return result | {'status': 'preflight_score_missing'}
    if not all(type(value) in (str, int) and re.fullmatch(r'[0-9]{1,3}', str(value)) for value in observed):
        return result | {'status': 'reference_score_missing'}
    actual = list(map(int, observed))
    result |= {'score': actual, 'status': 'observed_score_matches' if actual == wanted else 'score_mismatch'}
    context = result['event_context']
    if (actual == wanted and context['red_card_status'] == 'source_counts_match'
            and context.get('delay_status') != 'delay_unresolved'
            and context.get('latest_material_event_at')):
        result['verified_event_state'] = {
            'period': {1: 'first_half', 2: 'second_half'}[period], 'status': 'playing',
            'home_score': actual[0], 'away_score': actual[1],
            'home_red_cards': context['red_cards']['home'], 'away_red_cards': context['red_cards']['away'],
            'confirmed_at': context['latest_material_event_at'], 'received_at': result['received_at'],
        }
    return result


def compare_kalshi(sample, milestone, cutoff, max_age_seconds):
    observation = sample.get('kalshi')
    if not observation:
        return {'status': 'missing_observation'}
    if observation.get('milestone') != milestone:
        return {'status': 'fixture_mismatch'}
    received = instant(observation['received_at'])
    if received > cutoff or instant(observation['requested_at']) > received:
        return {'status': 'invalid_observation_time'}
    if cutoff - received > timedelta(seconds=max_age_seconds) or observation.get('live_data_delayed') is True:
        return {'status': 'delayed_observation'}
    if observation.get('error'):
        return {'status': 'read_failed'}
    scores = [observation.get(key) for key in ('home_same_game_score', 'away_same_game_score')]
    if any(type(score) is not int or score < 0 for score in scores):
        return {'status': 'score_missing'}
    phase = observation.get('match_status')
    result = {'received_at': observation['received_at'], 'score': scores, 'native_status': phase}
    if (phase == 'halftime' or sample['status'].get('type', {}).get('name') == 'STATUS_HALFTIME'):
        return result | {'status': 'halftime_observed'}
    if sample['status'].get('type', {}).get('state') == 'in' and phase in {'not_started', 'ended'}:
        return result | {'status': 'phase_conflict'}
    expected = [str(sample['teams'][side][1]) for side in ('home', 'away')]
    if not all(re.fullmatch(r'[0-9]{1,3}', score) for score in expected):
        return result | {'status': 'espn_score_missing'}
    return result | {'status': 'same_observed_score' if scores == list(map(int, expected)) else 'score_conflict'}


def native_book_times(observation, cutoff):
    books = observation.get('books', [])
    if not books or any(b.get('status') != 'two_sided' for b in books):
        return 'missing_depth', []
    if observation['source'] == 'polymarket' and observation.get('accepting_orders') is not True:
        return 'market_not_open', []
    if observation['source'] == 'polymarket_us' and any(b.get('market_state') != 'MARKET_STATE_OPEN' for b in books):
        return 'market_not_open', []
    if observation['source'] == 'kalshi' and observation.get('status') != 'active':
        return 'market_not_open', []
    received = [instant(b['received_at']) for b in books]
    if max(received) > cutoff:
        return 'received_after_preflight', []
    if any(not b.get('quote_time') for b in books):
        return 'missing_native_time', received
    native = [instant(b['quote_time']) for b in books]
    if any(quoted > arrival for quoted, arrival in zip(native, received)):
        return 'invalid_native_time', received
    return 'timed_depth', native


def replay_books(observation):
    books = observation['books']
    expected = {'polymarket': 2, 'polymarket_us': 1, 'kalshi': 1}.get(observation['source'])
    labels = [book.get('outcome') for book in books]
    if (len(labels) != expected or any(not isinstance(label, str) or not label.strip() for label in labels)
            or len(set(labels)) != len(labels)):
        raise ValueError('missing or ambiguous book outcome')

    rows = []
    for book in books:
        bid, ask, bid_size, ask_size = (Decimal(str(book[key])) for key in ('bid', 'ask', 'bid_size', 'ask_size'))
        if (not all(value.is_finite() for value in (bid, ask, bid_size, ask_size))
                or not 0 < bid < ask < 1 or min(bid_size, ask_size) <= 0):
            raise ValueError('invalid order book depth')
        rows.append({'outcome': book['outcome'], 'bid': str(bid), 'ask': str(ask),
                     'bid_size': str(bid_size), 'ask_size': str(ask_size),
                     'quoted_at': book['quote_time'], 'received_at': book['received_at']})
    return rows


def bind_outcomes(reference, orientation, kind, binary_winner=False):
    if orientation is None:
        return 'mapping_missing'
    if orientation['rules_sha256'] != reference['rules_sha256']:
        return 'mapping_rules_changed'
    labels = orientation['labels']
    if labels.keys() != {book['outcome'] for book in reference['books']}:
        return 'mapping_labels_changed'
    sides = set(labels.values())
    if len(sides) != len(labels):
        return 'mapping_invalid'
    selection = orientation.get('selection')
    expected = REFERENCE_SIDES.get(kind, set())
    kalshi_binary = reference.get('source') == 'kalshi' and set(labels) == {'yes'}
    yes_only = reference.get('source') == 'polymarket_us' and kind == 'winner' and not binary_winner
    proposition = kind == 'winner' and not binary_winner
    if kalshi_binary:
        pair = {'yes', 'no'} if proposition else {'home', 'away'} if binary_winner else expected
        if len(pair) != 2 or not sides < pair or proposition and sides != {'yes'}:
            return 'mapping_invalid'
        opposite, = pair - sides
        sides = pair
    if binary_winner:
        if selection is not None or sides != {'home', 'away'}:
            return 'mapping_invalid'
    elif kind == 'winner':
        if selection not in expected or sides != ({'yes'} if yes_only else {'yes', 'no'}):
            return 'mapping_invalid'
        reference['proposition'] = selection
    elif selection is not None or not sides <= expected:
        return 'mapping_invalid'
    for book in reference['books']:
        book['canonical_outcome'] = labels[book['outcome']]
    if kalshi_binary:
        yes, = reference['books']
        # The same matched binary book backs both sides at complementary prices.
        reference['books'].append(yes | {
            'outcome': 'no', 'canonical_outcome': opposite,
            'bid': str(1 - Decimal(yes['ask'])), 'ask': str(1 - Decimal(yes['bid'])),
            'bid_size': yes['ask_size'], 'ask_size': yes['bid_size'],
        })
    return 'mapped_yes_only' if yes_only else 'mapped'


def source_groups(references, kind, binary_winner=False):
    groups = {}
    for source in sorted({reference['source'] for reference in references}):
        markets = {}
        duplicates = set()
        expected = {'home', 'away'} if binary_winner else REFERENCE_SIDES.get(kind, set())
        mapped_status = ('mapped_yes_only' if source == 'polymarket_us' and kind == 'winner' and not binary_winner
                         else 'mapped')
        for reference in references:
            if reference['source'] != source or reference.get('outcome_status') != mapped_status:
                continue
            sides = ([reference['proposition']] if kind == 'winner' and not binary_winner
                     else [book['canonical_outcome'] for book in reference['books']])
            for side in sides:
                if side in markets:
                    duplicates.add(side)
                else:
                    markets[side] = reference['market']
        missing = expected - markets.keys()
        split_binary = (kind != 'winner' or binary_winner) and len(set(markets.values())) > 1
        status = 'ambiguous' if duplicates or split_binary else 'incomplete' if missing or not markets else 'complete'
        if status == 'complete' and mapped_status == 'mapped_yes_only':
            status = 'complete_yes_books_only'
        groups[source] = {'status': status, 'markets': markets, 'missing': sorted(missing),
                          'duplicates': sorted(duplicates)}
    return groups


def quote_at(binding, records, cutoff, earliest, outcomes, binary_winner=False):
    observed, observation = next((r for r in records if r[0] <= cutoff), (None, None))
    source = {'source': binding['source'], 'market': binding['market']}
    if observation is None:
        status = 'no_observation_before_preflight'
    elif observed < earliest:
        status = 'stale_before_preflight'
    elif observation.get('error'):
        status = 'source_read_failed'
    elif observation.get('rules_sha256') != binding['rules_sha256']:
        status = 'different_rules'
    else:
        status, timestamps = native_book_times(observation, cutoff)
        if timestamps and min(timestamps) < earliest:
            status = 'stale_before_preflight'
    if status == 'timed_depth':
        try:
            books = replay_books(observation)
        except (InvalidOperation, KeyError, TypeError, ValueError):
            status = 'invalid_depth_evidence'
        else:
            first, last = min(timestamps), max(timestamps)
            source |= {'oldest_quote_age_seconds': (cutoff - first).total_seconds(),
                       'within_market_skew_seconds': (last - first).total_seconds(),
                       'rules_sha256': observation['rules_sha256'], 'books': books}
            if outcomes is not None:
                orientation = outcomes.get(binding['source'], {}).get(binding['market'])
                source['outcome_status'] = bind_outcomes(source, orientation, binding['kind'], binary_winner)
            event = observation.get('event_state')
            if (isinstance(event, dict) and event.get('fixture_id') == binding['fixture_id']
                    and event.get('source') == binding['source']):
                source['verified_event_state'] = event
    return source | {'status': status}


def adjacent_groups(contract, lookup, quote, max_skew):
    fixture, kind, period, team_side, line = contract
    if (kind not in {'total', 'team_total', 'handicap'} or line is None or not line.is_finite()
            or line % Decimal('0.25')):
        return None
    if abs(line) % 1 == Decimal('0.5'):
        return None
    lines, _ = adjacent_lines(kind, line)
    references = [[quote(binding) for binding in lookup.get((fixture, kind, period, team_side, half), [])]
                  for half in lines]
    groups = [source_groups(quotes, kind) for quotes in references]
    sources = {}
    for source in sorted(groups[0].keys() | groups[1].keys()):
        states = [group.get(source, {}).get('status', 'missing_binding') for group in groups]
        entry = {'line_statuses': states, 'status': 'incomplete'}
        if 'ambiguous' in states:
            entry['status'] = 'ambiguous'
        elif states == ['complete', 'complete']:
            times = [instant(book['quoted_at']) for quotes in references for ref in quotes
                     if ref['source'] == source and ref.get('outcome_status') == 'mapped' for book in ref['books']]
            skew = (max(times) - min(times)).total_seconds()
            entry |= {'quote_skew_seconds': skew, 'status': 'paired' if skew <= max_skew else 'different_quote_times'}
        sources[source] = entry
    return {'lines': [{'line': str(half), 'references': quotes} for half, quotes in zip(lines, references)],
            'source_groups': sources, 'max_native_skew_seconds': max_skew}


def index_observations(archive, cutoffs):
    by_market = {}
    for batch in archive:
        observations = batch['markets']
        if not cutoffs:
            continue
        for observation in observations:
            key = observation['source'], observation['market']
            observed = max((instant(book['received_at']) for book in observation.get('books', [])),
                           default=instant(observation.get('observed_at') or batch['captured_at']))
            bucket = bisect_left(cutoffs, observed)
            if bucket == len(cutoffs):
                continue
            records = by_market.setdefault(key, {})
            prior = records.get(bucket)
            if prior is None or observed > prior[0]:
                records[bucket] = (observed, observation)
    return {key: [record for _, record in sorted(records.items(), reverse=True)]
            for key, records in by_market.items()}


def compare(preflights, bindings, archive, max_age_seconds, outcomes=None, adjacent_skew_seconds=5,
            *, score_records=(), score_bindings=None, score_max_age_seconds=45, stream_segments=None):
    cutoffs = sorted({instant(row['captured_at']) for row in preflights})
    archive_by_market = index_observations(archive, cutoffs)
    by_contract = {}
    for binding in bindings:
        contract = contract_key(binding)
        if binding['kind'] != 'team_total' or contract[3] in {'home', 'away'}:
            by_contract.setdefault(contract, []).append(binding)
    score_records = sorted(((instant(row['received_at']), row) for row in score_records),
                           key=lambda item: item[0], reverse=True)
    rows = []
    for preflight in preflights:
        cutoff = instant(preflight['captured_at'])
        earliest = cutoff - timedelta(seconds=max_age_seconds)
        equivalent = full_match_line(preflight)
        reference_offer = preflight if equivalent is None else preflight | {'line': equivalent}
        contract = contract_key(reference_offer)
        binary_winner = preflight.get('sport') == 'basketball' and preflight['kind'] == 'winner'
        score_comparison = None
        if preflight.get('sport') == 'football' and preflight.get('phase') == 'live':
            score_comparison = match_scores(preflight, (score_bindings or {}).get(preflight['fixture_id']),
                                            score_records, cutoff, score_max_age_seconds)
        event_context = (score_comparison or {}).get('event_context', {})

        def quote(binding):
            records = archive_by_market.get((binding['source'], binding['market']), [])
            reference = quote_at(binding, records, cutoff, earliest, outcomes, binary_winner)
            latest_event = event_context.get('latest_material_event_at')
            if latest_event and reference['status'] == 'timed_depth':
                first_quote = min(instant(book['quoted_at']) for book in reference['books'])
                reference['live_event_quote_status'] = (
                    'quote_predates_known_event' if first_quote < instant(latest_event) else 'quote_after_known_event'
                )
            if stream_segments is not None and binding['source'] == 'polymarket':
                stream = stream_quote(binding, stream_segments, cutoff, max_age_seconds)
                compare_rest(stream, reference)
                reference['stream'] = stream
            return reference

        remaining_unverified = equivalent is None and preflight.get('effective_score_basis') == 'remaining_period'
        sources = [] if remaining_unverified else [quote(binding) for binding in by_contract.get(contract, [])]
        row = {'preflight': preflight, 'references': sources,
               'settlement_comparison': settlement_comparison(preflight)}
        if equivalent is not None:
            row['equivalent_full_match_line'] = equivalent
        if score_comparison is not None:
            row['score_comparison'] = score_comparison
            if 'verified_event_state' in score_comparison:
                row['verified_offer_event_state'] = score_comparison['verified_event_state']
        if outcomes is not None and not remaining_unverified:
            row['source_groups'] = source_groups(sources, preflight['kind'], binary_winner)
            adjacent = adjacent_groups(contract, by_contract, quote, min(adjacent_skew_seconds, max_age_seconds))
            if adjacent is not None:
                row['adjacent_line_coverage'] = adjacent
            if preflight.get('sport') == 'football' and preflight['kind'] == 'handicap' and contract[-1] == 0:
                winner = (contract[0], 'winner', contract[2], None, None)
                winner_refs = [quote(binding) for binding in by_contract.get(winner, [])]
                if winner_refs:
                    row['winner_reference_for_zero_handicap'] = {
                        'references': winner_refs, 'source_groups': source_groups(winner_refs, 'winner'),
                    }
        rows.append(row)
    return rows


def archive_records(paths, stats):
    for path in paths:
        with path.open('rb') as records:
            for line in records:
                if not line.endswith(b'\n'):
                    stats['interrupted'] += 1
                    break
                record = json.loads(line)
                stats['rounds'] += 1
                yield record


def read_archive(paths):
    stats = dict(rounds=0, interrupted=0)
    return list(archive_records(paths, stats)), stats['interrupted']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('preflights', type=Path)
    parser.add_argument('bindings', type=Path)
    parser.add_argument('archive', nargs='+', type=Path)
    parser.add_argument('--max-age-seconds', type=int, default=30)
    parser.add_argument('--outcomes', type=Path)
    parser.add_argument('--score-bindings', type=Path)
    parser.add_argument('--score-archive', action='append', type=Path, default=[])
    parser.add_argument('--stream-archive', action='append', type=Path, default=[])
    parser.add_argument('--pricing-approvals', type=Path)
    parser.add_argument('--catalog-expansion', type=Path)
    args = parser.parse_args()
    if not 1 <= args.max_age_seconds <= 120:
        parser.error('max age must be 1-120 seconds')
    try:
        preflights = json.loads(args.preflights.read_text())
        bindings = json.loads(args.bindings.read_text())
        outcomes = json.loads(args.outcomes.read_text()) if args.outcomes else None
        if args.outcomes and not isinstance(outcomes, dict):
            raise ValueError('invalid outcome manifest')
        existing = {(binding['source'], binding['market']) for binding in bindings}
        fixture_ids = {entry['fixture_id'] for entry in bindings}
        for market, orientation in (outcomes or {}).get('polymarket_us', {}).items():
            if orientation['fixture_id'] in fixture_ids and ('polymarket_us', market) not in existing:
                bindings.append({'source': 'polymarket_us', 'market': market, 'kind': 'winner',
                                 'period': 'full_time', 'line': None, 'fixture_id': orientation['fixture_id'],
                                 'rules_sha256': orientation['rules_sha256']})
        if args.catalog_expansion:
            supplement = json.loads(args.catalog_expansion.read_bytes())
            supplemental_outcomes = supplement['outcomes']['polymarket']
            outcomes = outcomes or {}
            primary = outcomes.get('polymarket', {})
            supplemental_bindings = supplement['bindings']
            existing = {(binding['source'], binding['market']) for binding in bindings}
            keys = {(binding['source'], binding['market']) for binding in supplemental_bindings}
            if (keys != {('polymarket', market) for market in supplemental_outcomes}
                    or existing & keys or len(keys) != len(supplemental_bindings) or primary.keys() & supplemental_outcomes.keys()):
                raise ValueError('invalid or duplicate catalog expansion')
            bindings.extend(supplemental_bindings)
            outcomes['polymarket'] = primary | supplemental_outcomes
        approvals_raw = args.pricing_approvals.read_bytes() if args.pricing_approvals else None
        approvals = json.loads(approvals_raw) if approvals_raw is not None else []
        if not isinstance(approvals, list):
            raise ValueError('invalid pricing approvals')
        score_bindings = json.loads(args.score_bindings.read_text()) if args.score_bindings else {}
        if not isinstance(score_bindings, dict) or (args.score_archive and not score_bindings):
            raise ValueError('invalid score bindings')
        order_book_stats = dict(rounds=0, interrupted=0)
        scores, interrupted_scores = read_archive(args.score_archive)
        cutoffs = sorted({instant(row['captured_at']) for row in preflights})
        stream_segments, stream_summaries = [], []
        for path in args.stream_archive:
            try:
                segment = read_segment(path, cutoffs)
            except (OSError, KeyError, TypeError, ValueError, InvalidOperation) as error:
                stream_summaries.append({'archive': path.name, 'error': type(error).__name__})
            else:
                stream_segments.append(segment)
                stream_summaries.append(segment[2])
        matches = compare(preflights, bindings, archive_records(args.archive, order_book_stats), args.max_age_seconds, outcomes,
                          score_records=scores, score_bindings=score_bindings,
                          stream_segments=stream_segments if args.stream_archive else None)
        if not order_book_stats['rounds']:
            raise ValueError('no captured order book rounds')
        for row in matches:
            row['price_decision'] = evaluate(row, approvals, args.max_age_seconds)
        print(json.dumps({'capture_rounds': order_book_stats['rounds'], 'max_age_seconds': args.max_age_seconds,
                          'archives_with_incomplete_tail': order_book_stats['interrupted'],
                          'score_capture_rounds': len(scores), 'score_archives_with_incomplete_tail': interrupted_scores,
                          'pricing_approvals_sha256': (
                              sha256(approvals_raw).hexdigest() if approvals_raw is not None else None),
                          'automatic_policy_comparison': compare_with_receipts(matches),
                          'stream_segments': stream_summaries,
                          'preflights': matches}, default=str))
    except (OSError, KeyError, TypeError, ValueError, InvalidOperation) as error:
        parser.exit(1, f'Invalid preflight evidence ({type(error).__name__})\n')


if __name__ == '__main__':
    main()
