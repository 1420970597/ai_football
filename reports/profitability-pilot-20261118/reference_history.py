"""Read historical prices for manually matched public market IDs."""

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GAMMA = 'https://gamma-api.polymarket.com'
CLOB = 'https://clob.polymarket.com'
KALSHI = 'https://api.elections.kalshi.com/trade-api/v2'
LOOKBACK = timedelta(minutes=10)


def get_json(url, *, body=None, **params):
    if params:
        url += '?' + urlencode(params)
    headers = {'User-Agent': 'ai-football-reference-research/1.0'}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    request = Request(url, data=data, headers=headers)
    limit = 512 * 1024
    with urlopen(request, timeout=8) as response:
        payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ValueError('reference response exceeds size limit')
    return json.loads(payload, parse_float=Decimal)


def at_or_before(rows, timestamp_key, cutoff):
    earliest = (cutoff - LOOKBACK).timestamp()
    latest = cutoff.timestamp()
    candidates = (row for row in rows if earliest <= row[timestamp_key] <= latest)
    return max(candidates, key=lambda row: row[timestamp_key], default=None)


def point_time(timestamp, cutoff):
    recorded_at = datetime.fromtimestamp(timestamp, timezone.utc)
    return {'time': recorded_at.isoformat(), 'age_seconds': (cutoff - recorded_at).total_seconds()}


def polymarket_history(slug, cutoff):
    market = get_json(f'{GAMMA}/markets/slug/{slug}')
    sides = json.loads(market['outcomes'])
    tokens = json.loads(market['clobTokenIds'])
    if len(sides) != 2 or len(set(sides)) != 2 or len(set(tokens)) != 2 or len(tokens) != 2:
        raise ValueError('expected a two-outcome Polymarket contract')
    history = []
    for side, token in zip(sides, tokens):
        timeline = get_json(f'{CLOB}/prices-history', market=token, fidelity=1,
                            startTs=int((cutoff - LOOKBACK).timestamp()), endTs=int(cutoff.timestamp()))
        point = at_or_before(timeline['history'], 't', cutoff)
        history.append({'outcome': side, 'point': None if point is None else {
            **point_time(point['t'], cutoff), 'price': point['p'],
        }})
    return {'market': slug, 'question': market['question'], 'game_start': market.get('gameStartTime'),
            'market_type': market.get('sportsMarketType'), 'line': market.get('line'),
            'observation_kind': 'token_price_history', 'outcomes': history}


def kalshi_history(series, ticker, cutoff):
    market = get_json(f'{KALSHI}/markets/{ticker}')['market']
    candle = at_or_before(kalshi_candles(series, ticker, cutoff), 'end_period_ts', cutoff)
    return {'market': ticker, 'title': market['title'], 'strike_type': market.get('strike_type'),
            'floor_strike': market.get('floor_strike'), 'latest_close_time': market.get('close_time'),
            'observation_kind': 'closed_one_minute_candle', 'candle': None if candle is None else {
                **point_time(candle['end_period_ts'], cutoff),
                **{key: candle[key].get('close_dollars') for key in ('yes_bid', 'yes_ask', 'price')},
            }}


def kalshi_candles(series, ticker, cutoff):
    return get_json(f'{KALSHI}/series/{series}/markets/{ticker}/candlesticks',
                    start_ts=int((cutoff - LOOKBACK).timestamp()), end_ts=int(cutoff.timestamp()),
                    period_interval=1)['candlesticks']


def shared_kalshi_candle(candles_by_ticker, cutoff):
    """Return the latest common closed minute with both sides of each quote."""
    if not 2 <= len(candles_by_ticker) <= 3:
        raise ValueError('expected two or three distinct contracts')
    earliest = (cutoff - LOOKBACK).timestamp()
    latest = cutoff.timestamp()
    books = {}
    for ticker, candles in candles_by_ticker.items():
        intervals = {}
        seen = set()
        for row in candles:
            stamp = row['end_period_ts']
            if type(stamp) is not int or stamp % 60 or not earliest <= stamp <= latest:
                continue
            if stamp in seen:
                intervals.pop(stamp, None)
                continue
            seen.add(stamp)
            try:
                bid = Decimal(row['yes_bid']['close_dollars'])
                ask = Decimal(row['yes_ask']['close_dollars'])
            except (ArithmeticError, KeyError, TypeError, ValueError):
                continue
            if bid.is_finite() and ask.is_finite() and 0 < bid <= ask < 1:
                intervals[stamp] = {'yes_bid': bid, 'yes_ask': ask}
        books[ticker] = intervals
    shared = set.intersection(*(set(intervals) for intervals in books.values()))
    result = {'markets': list(books), 'observation_kind': 'shared_closed_one_minute_candle', 'candle': None}
    if shared:
        stamp = max(shared)
        result['candle'] = {**point_time(stamp, cutoff), 'quotes': {
            ticker: intervals[stamp] for ticker, intervals in books.items()
        }}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cutoff', type=datetime.fromisoformat)
    parser.add_argument('polymarket_slug')
    parser.add_argument('kalshi_series')
    parser.add_argument('kalshi_ticker')
    parser.add_argument('--kalshi-peer-ticker', action='append', default=[])
    args = parser.parse_args()
    if args.cutoff.utcoffset() != timedelta(0) or args.cutoff >= datetime.now(timezone.utc):
        parser.error('cutoff must be a past UTC timestamp')
    if not re.fullmatch(r'[a-z0-9-]{1,160}', args.polymarket_slug):
        parser.error('invalid Polymarket slug')
    tickers = [args.kalshi_ticker, *args.kalshi_peer_ticker]
    if (not re.fullmatch(r'[A-Z0-9]{1,40}', args.kalshi_series)
            or len(tickers) > 3 or len(set(tickers)) != len(tickers)
            or any(not re.fullmatch(r'[A-Z0-9-]{1,160}', ticker)
                   or not ticker.startswith(args.kalshi_series + '-') for ticker in tickers)):
        parser.error('invalid Kalshi series or ticker')
    report = {'cutoff': args.cutoff.isoformat(), 'purpose': 'historical_research',
              'polymarket': polymarket_history(args.polymarket_slug, args.cutoff)}
    if args.kalshi_peer_ticker:
        report['kalshi'] = shared_kalshi_candle({
            ticker: kalshi_candles(args.kalshi_series, ticker, args.cutoff) for ticker in tickers
        }, args.cutoff)
    else:
        report['kalshi'] = kalshi_history(args.kalshi_series, args.kalshi_ticker, args.cutoff)
    report['queried_at'] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(report, default=str))


if __name__ == '__main__':
    main()
