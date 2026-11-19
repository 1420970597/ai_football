"""Measure quote coverage at genuine preflight cutoffs from private archives."""

import argparse
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path


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


def compare(preflights, bindings, archive, max_age_seconds):
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
            source['status'] = status
            if status == 'timed_depth':
                first, last = min(timestamps), max(timestamps)
                source |= {'oldest_quote_age_seconds': (cutoff - first).total_seconds(),
                           'within_market_skew_seconds': (last - first).total_seconds()}
            sources.append(source)
        rows.append({'preflight': preflight, 'references': sources})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('preflights', type=Path)
    parser.add_argument('bindings', type=Path)
    parser.add_argument('archive', nargs='+', type=Path)
    parser.add_argument('--max-age-seconds', type=int, default=30)
    args = parser.parse_args()
    if not 1 <= args.max_age_seconds <= 120:
        parser.error('max age must be 1-120 seconds')
    try:
        preflights = json.loads(args.preflights.read_text())
        bindings = json.loads(args.bindings.read_text())
        archive = []
        for path in args.archive:
            with path.open() as records:
                archive.extend(json.loads(line) for line in records)
        if not archive:
            raise ValueError('no captured order book rounds')
        matches = compare(preflights, bindings, archive, args.max_age_seconds)
        print(json.dumps({'capture_rounds': len(archive), 'max_age_seconds': args.max_age_seconds,
                          'preflights': matches}))
    except (OSError, KeyError, TypeError, ValueError, InvalidOperation) as error:
        parser.exit(1, f'Invalid preflight evidence ({type(error).__name__})\n')


if __name__ == '__main__':
    main()
