"""Read historical quotes for two manually matched public market IDs."""

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


def get_json(url, **params):
    if params:
        url += '?' + urlencode(params)
    request = Request(url, headers={'User-Agent': 'ai-football-reference-research/1.0'})
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
    timeline = get_json(f'{KALSHI}/series/{series}/markets/{ticker}/candlesticks',
                        start_ts=int((cutoff - LOOKBACK).timestamp()), end_ts=int(cutoff.timestamp()),
                        period_interval=1)
    candle = at_or_before(timeline['candlesticks'], 'end_period_ts', cutoff)
    return {'market': ticker, 'title': market['title'], 'strike_type': market.get('strike_type'),
            'floor_strike': market.get('floor_strike'), 'latest_close_time': market.get('close_time'),
            'observation_kind': 'closed_one_minute_candle', 'candle': None if candle is None else {
                **point_time(candle['end_period_ts'], cutoff),
                **{key: candle[key].get('close_dollars') for key in ('yes_bid', 'yes_ask', 'price')},
            }}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cutoff', type=datetime.fromisoformat)
    parser.add_argument('polymarket_slug')
    parser.add_argument('kalshi_series')
    parser.add_argument('kalshi_ticker')
    args = parser.parse_args()
    if args.cutoff.utcoffset() != timedelta(0) or args.cutoff >= datetime.now(timezone.utc):
        parser.error('cutoff must be a past UTC timestamp')
    if not re.fullmatch(r'[a-z0-9-]{1,160}', args.polymarket_slug):
        parser.error('invalid Polymarket slug')
    if (not re.fullmatch(r'[A-Z0-9]{1,40}', args.kalshi_series)
            or not re.fullmatch(r'[A-Z0-9-]{1,160}', args.kalshi_ticker)
            or not args.kalshi_ticker.startswith(args.kalshi_series + '-')):
        parser.error('invalid Kalshi series or ticker')
    report = {'cutoff': args.cutoff.isoformat(), 'purpose': 'historical_research',
              'polymarket': polymarket_history(args.polymarket_slug, args.cutoff),
              'kalshi': kalshi_history(args.kalshi_series, args.kalshi_ticker, args.cutoff),
              'queried_at': datetime.now(timezone.utc).isoformat()}
    print(json.dumps(report, default=str))


if __name__ == '__main__':
    main()
