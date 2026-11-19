"""Record bounded public Polymarket book events with original receive times."""

import argparse
from datetime import datetime, timezone
import json
import re
import sys
from time import monotonic

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from reference_orderbooks import polymarket_metadata


ENDPOINT = 'wss://ws-subscriptions-clob.polymarket.com/ws/market'
ARCHIVE_LIMIT = 16 * 1024 * 1024


def book_event(row, received, conditions, initialized):
    kind = row.get('event_type')
    if kind not in {'book', 'price_change', 'best_bid_ask', 'last_trade_price', 'tick_size_change'}:
        return None
    stamp = str(row['timestamp'])
    if not re.fullmatch(r'[0-9]{13}', stamp) or int(stamp) > received.timestamp() * 1000:
        raise ValueError('invalid native market event time')
    entries = row['price_changes'] if kind == 'price_change' else [row]
    if not entries or any(conditions.get(entry['asset_id']) != row['market']
                          or entry['asset_id'] not in conditions for entry in entries):
        raise ValueError('unexpected market event identity')
    if kind == 'book':
        asset = row['asset_id']
        kind = 'book_update' if asset in initialized else 'initial_book'
        initialized.add(asset)
    return {'kind': kind, 'received_at': received.isoformat(), 'event': row}


def capture(markets, seconds):
    conditions = {}
    for slug in markets:
        metadata, sides = polymarket_metadata(slug)
        condition = metadata['condition_id']
        for _, asset in sides:
            if asset in conditions:
                raise ValueError('duplicate subscription asset')
            conditions[asset] = condition
        yield {'kind': 'subscription_market', 'assets': dict(sides),
               'market': slug, 'condition_id': condition, 'rules_sha256': metadata['rules_sha256']}
    initialized = set()
    with connect(ENDPOINT, open_timeout=8, close_timeout=2, max_size=1024 * 1024) as socket:
        socket.send(json.dumps({'assets_ids': list(conditions), 'type': 'market', 'custom_feature_enabled': True}))
        deadline = monotonic() + seconds
        heartbeat = monotonic() + 8
        while (current := monotonic()) < deadline:
            if current >= heartbeat:
                socket.send('PING')
                heartbeat = current + 8
            try:
                message = socket.recv(timeout=min(2, deadline - current))
            except TimeoutError:
                continue
            received = datetime.now(timezone.utc)
            if message == 'PONG':
                continue
            decoded = json.loads(message)
            for row in decoded if isinstance(decoded, list) else [decoded]:
                if not isinstance(row, dict):
                    raise ValueError('unexpected public stream frame')
                record = book_event(row, received, conditions, initialized)
                if record is not None:
                    yield record
    if initialized != conditions.keys():
        raise ValueError('initial subscription books are incomplete')
    yield {'kind': 'observation_end', 'assets_initialized': len(initialized), 'observation_seconds': seconds}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('markets', nargs='+')
    parser.add_argument('--seconds', type=int, default=60)
    args = parser.parse_args()
    if (not 5 <= args.seconds <= 900 or not 1 <= len(args.markets) <= 8
            or len(set(args.markets)) != len(args.markets)
            or any(not re.fullmatch(r'[a-z0-9-]{1,160}', slug) for slug in args.markets)):
        parser.error('provide one to eight public markets and a duration of 5 to 900 seconds')
    size = 0
    try:
        for row in capture(args.markets, args.seconds):
            payload = json.dumps(row, separators=(',', ':'))
            size += len(payload) + 1
            if size > ARCHIVE_LIMIT:
                raise ValueError('stream archive size limit exceeded')
            print(payload, flush=True)
    except (KeyError, TypeError, OSError, ValueError, WebSocketException) as error:
        print(f'Public market observation interrupted ({type(error).__name__}): {error}', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
