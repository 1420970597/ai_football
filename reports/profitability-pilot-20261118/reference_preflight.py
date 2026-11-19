"""Measure quote coverage at genuine preflight cutoffs from private archives."""

import argparse
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path

REFERENCE_SIDES = {'winner': {'home', 'draw', 'away'}, 'handicap': {'home', 'away'},
                   'total': {'over', 'under'}, 'team_total': {'over', 'under'}}


def instant(value):
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError('timestamp must include a timezone')
    return result


def contract_key(row):
    line = row.get('line')
    return row['fixture_id'], row['kind'], row['period'], Decimal(str(line)) if line is not None else None


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


def bind_outcomes(reference, orientation, kind):
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
    if kind == 'winner':
        if selection not in expected or sides != {'yes', 'no'}:
            return 'mapping_invalid'
        reference['proposition'] = selection
    elif selection is not None or not sides <= expected:
        return 'mapping_invalid'
    for book in reference['books']:
        book['canonical_outcome'] = labels[book['outcome']]
    return 'mapped'


def source_groups(references, kind):
    groups = {}
    for source in sorted({reference['source'] for reference in references}):
        markets = {}
        duplicates = set()
        expected = REFERENCE_SIDES.get(kind, set())
        for reference in references:
            if reference['source'] != source or reference.get('outcome_status') != 'mapped':
                continue
            sides = ([reference['proposition']] if kind == 'winner'
                     else [book['canonical_outcome'] for book in reference['books']])
            for side in sides:
                if side in markets:
                    duplicates.add(side)
                else:
                    markets[side] = reference['market']
        missing = expected - markets.keys()
        split_binary = kind != 'winner' and len(set(markets.values())) > 1
        status = 'ambiguous' if duplicates or split_binary else 'incomplete' if missing or not markets else 'complete'
        groups[source] = {'status': status, 'markets': markets, 'missing': sorted(missing),
                          'duplicates': sorted(duplicates)}
    return groups


def compare(preflights, bindings, archive, max_age_seconds, outcomes=None):
    archive_by_market = {}
    for batch in archive:
        for observation in batch['markets']:
            key = observation['source'], observation['market']
            observed = max((instant(book['received_at']) for book in observation.get('books', [])),
                           default=instant(observation.get('observed_at') or batch['captured_at']))
            archive_by_market.setdefault(key, []).append((observed, observation))
    for records in archive_by_market.values():
        records.sort(key=lambda record: record[0], reverse=True)
    by_contract = {}
    for binding in bindings:
        by_contract.setdefault(contract_key(binding), []).append(binding)
    rows = []
    for preflight in preflights:
        cutoff = instant(preflight['captured_at'])
        earliest = cutoff - timedelta(seconds=max_age_seconds)
        sources = []
        for binding in by_contract.get(contract_key(preflight), []):
            key = binding['source'], binding['market']
            observed, observation = next((r for r in archive_by_market.get(key, []) if r[0] <= cutoff), (None, None))
            source = {'source': key[0], 'market': key[1]}
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
                        source['outcome_status'] = bind_outcomes(source, orientation, preflight['kind'])
            source['status'] = status
            sources.append(source)
        row = {'preflight': preflight, 'references': sources}
        if outcomes is not None:
            row['source_groups'] = source_groups(sources, preflight['kind'])
        rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('preflights', type=Path)
    parser.add_argument('bindings', type=Path)
    parser.add_argument('archive', nargs='+', type=Path)
    parser.add_argument('--max-age-seconds', type=int, default=30)
    parser.add_argument('--outcomes', type=Path)
    args = parser.parse_args()
    if not 1 <= args.max_age_seconds <= 120:
        parser.error('max age must be 1-120 seconds')
    try:
        preflights = json.loads(args.preflights.read_text())
        bindings = json.loads(args.bindings.read_text())
        outcomes = json.loads(args.outcomes.read_text()) if args.outcomes else None
        if args.outcomes and not isinstance(outcomes, dict):
            raise ValueError('invalid outcome manifest')
        archive = []
        interrupted = 0
        for path in args.archive:
            with path.open('rb') as records:
                for line in records:
                    if not line.endswith(b'\n'):
                        interrupted += 1
                        break
                    archive.append(json.loads(line))
        if not archive:
            raise ValueError('no captured order book rounds')
        matches = compare(preflights, bindings, archive, args.max_age_seconds, outcomes)
        print(json.dumps({'capture_rounds': len(archive), 'max_age_seconds': args.max_age_seconds,
                          'archives_with_incomplete_tail': interrupted,
                          'preflights': matches}))
    except (OSError, KeyError, TypeError, ValueError, InvalidOperation) as error:
        parser.exit(1, f'Invalid preflight evidence ({type(error).__name__})\n')


if __name__ == '__main__':
    main()
