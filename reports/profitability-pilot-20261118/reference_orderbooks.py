"""Capture top-of-book evidence for manually matched public market IDs."""

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from http.client import HTTPException
import json
import re
from time import monotonic, sleep

from reference_history import CLOB, GAMMA, KALSHI, get_json


def now():
    return datetime.now(timezone.utc)


def top_level(levels, *, bid):
    valid = []
    for price, size in levels:
        price, size = Decimal(price), Decimal(size)
        if not (price.is_finite() and size.is_finite() and 0 < price < 1 and size > 0):
            raise ValueError('invalid depth level')
        valid.append((price, size))
    if not valid:
        return None
    return (max if bid else min)(valid, key=lambda level: level[0])


def top_book(bid, ask):
    if bid is None or ask is None:
        return {'status': 'missing_side'}
    if bid[0] >= ask[0]:
        return {'status': 'crossed_or_locked'}
    return {'status': 'two_sided', 'bid': bid[0], 'ask': ask[0], 'bid_size': bid[1], 'ask_size': ask[1]}


def polymarket_book(book, condition, token, received_at):
    if (book.get('market'), book.get('asset_id')) != (condition, token):
        raise ValueError('order book identity mismatch')
    timestamp = str(book.get('timestamp', ''))
    quoted_at = datetime.fromtimestamp(int(timestamp) // 1000, timezone.utc) + timedelta(
        milliseconds=int(timestamp) % 1000
    ) if re.fullmatch(r'[0-9]{13}', timestamp) else None
    bids = [(level['price'], level['size']) for level in book['bids']]
    asks = [(level['price'], level['size']) for level in book['asks']]
    result = top_book(top_level(bids, bid=True), top_level(asks, bid=False))
    return result | {
        'quote_time': quoted_at, 'quote_age_seconds': (received_at - quoted_at).total_seconds() if quoted_at else None,
        'snapshot_hash': book.get('hash'),
    }


def kalshi_book(book):
    # A NO bid at p is a YES ask at 1-p, with the same displayed size.
    levels = book['orderbook_fp']
    yes = top_level(levels.get('yes_dollars') or [], bid=True)
    no = top_level(levels.get('no_dollars') or [], bid=True)
    return top_book(yes, (1 - no[0], no[1]) if no else None) | {'quote_time': None}


def capture_market(source, identifier):
    start = now()
    if source == 'polymarket':
        market = get_json(f'{GAMMA}/markets/slug/{identifier}')
        condition = market['conditionId']
        labels = json.loads(market['outcomes'])
        tokens = json.loads(market['clobTokenIds'])
        if (market['slug'] != identifier or len(labels) != 2 or len(tokens) != 2
                or len(set(labels)) != 2 or len(set(tokens)) != 2):
            raise ValueError('unexpected market identity or outcome set')
        books = []
        for label, token in zip(labels, tokens):
            raw = get_json(f'{CLOB}/book', token_id=token)
            received_at = now()
            books.append(polymarket_book(raw, condition, token, received_at) | {
                'outcome': label, 'received_at': received_at
            })
        metadata = {
            'market_type': market.get('sportsMarketType'), 'line': market.get('line'),
            'scheduled_start': market.get('gameStartTime'), 'accepting_orders': market['acceptingOrders'],
            'condition_id': condition,
        }
        rules = market['description']
    else:
        market = get_json(f'{KALSHI}/markets/{identifier}')['market']
        if market['ticker'] != identifier:
            raise ValueError('market identity mismatch')
        raw = get_json(f'{KALSHI}/markets/{identifier}/orderbook', depth=1)
        books = [kalshi_book(raw) | {'outcome': 'yes', 'received_at': now()}]
        metadata = {
            'event_ticker': market['event_ticker'], 'status': market['status'],
            'line': market.get('floor_strike'), 'strike_type': market.get('strike_type'),
        }
        rules = '\n'.join((market['rules_primary'], market['rules_secondary']))
    return {
        'source': source, 'market': identifier, 'market_metadata_requested_at': start,
        'rules_sha256': hashlib.sha256(rules.encode()).hexdigest(), 'books': books, **metadata,
    }


def capture(markets):
    recorded = []
    for source, identifier in markets:
        try:
            recorded.append(capture_market(source, identifier))
        except (ArithmeticError, HTTPException, OSError, ValueError, KeyError, TypeError) as exc:
            recorded.append({'source': source, 'market': identifier, 'error': type(exc).__name__,
                             'http_status': getattr(exc, 'code', None), 'observed_at': now()})
    return {'purpose': 'market_data_observation', 'captured_at': now(), 'markets': recorded}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--polymarket', action='append', default=[])
    parser.add_argument('--kalshi', action='append', default=[])
    parser.add_argument('--samples', type=int, default=1)
    parser.add_argument('--interval-seconds', type=int, default=30)
    args = parser.parse_args()
    markets = [('polymarket', slug) for slug in args.polymarket] + [('kalshi', ticker) for ticker in args.kalshi]
    if (not 1 <= len(markets) <= 8 or len(set(markets)) != len(markets)
            or any(not re.fullmatch(r'[a-z0-9-]{1,160}', slug) for slug in args.polymarket)
            or any(not re.fullmatch(r'[A-Z0-9-]{1,160}', ticker) for ticker in args.kalshi)):
        parser.error('provide one to eight distinct public market IDs')
    if not 1 <= args.samples <= 20 or not 20 <= args.interval_seconds <= 300:
        parser.error('samples must be 1-20 and interval must be 20-300 seconds')
    for index in range(args.samples):
        start = monotonic()
        print(json.dumps(capture(markets), default=str), flush=True)
        if index + 1 < args.samples:
            sleep(max(0, args.interval_seconds - (monotonic() - start)))


if __name__ == '__main__':
    main()
