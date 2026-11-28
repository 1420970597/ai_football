"""Recover top-of-book prices and depth at preflight cutoffs within a connection."""

from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import re

from reference_orderbooks import top_level


def quote_entries(row):
    event = row['event']
    kind = row['kind']
    if kind in {'initial_book', 'book_update'}:
        bid = top_level(((level['price'], level['size']) for level in event['bids']), bid=True)
        ask = top_level(((level['price'], level['size']) for level in event['asks']), bid=False)
        yield event, bid[0] if bid else None, ask[0] if ask else None
    elif kind in {'price_change', 'best_bid_ask'}:
        for entry in event['price_changes'] if kind == 'price_change' else [event]:
            yield entry, entry.get('best_bid'), entry.get('best_ask')


def book_depth(books, kind, entry, bid, ask):
    asset = entry['asset_id']
    if kind in {'initial_book', 'book_update'}:
        levels = {
            side: {Decimal(level['price']): Decimal(level['size']) for level in entry[field]}
            for side, field in (('BUY', 'bids'), ('SELL', 'asks'))
        }
        if len(levels['BUY']) != len(entry['bids']) or len(levels['SELL']) != len(entry['asks']):
            books.pop(asset, None)
            return None
        books[asset] = levels
    if asset not in books:
        return None
    levels = books[asset]
    try:
        if kind == 'price_change':
            side = levels[entry['side']]
            price, size = Decimal(entry['price']), Decimal(entry['size'])
            if not (price.is_finite() and size.is_finite() and 0 < price < 1 and size >= 0):
                raise ValueError('invalid price change')
            if size:
                side[price] = size
            else:
                side.pop(price, None)
        if (not levels['BUY'] or not levels['SELL']
                or max(levels['BUY']) != Decimal(bid) or min(levels['SELL']) != Decimal(ask)):
            raise ValueError('reconstructed best prices do not match the feed')
    except (KeyError, TypeError, ValueError, InvalidOperation):
        del books[asset]
        return None
    if kind != 'best_bid_ask':
        return levels['BUY'][Decimal(bid)], levels['SELL'][Decimal(ask)]
    return None


def invalidate_depth(books, latest, asset):
    books.pop(asset, None)
    if asset in latest:
        latest[asset] = (*latest[asset][:4], None)


def read_segment(path, cutoffs):
    markets, tokens, latest, frames = {}, {}, {}, {}
    books = {}
    first = last = None
    next_cutoff = 0
    count = 0
    complete = False
    truncated = False
    with path.open('rb') as archive:
        for line in archive:
            if not line.endswith(b'\n'):
                truncated = True
                break
            row = json.loads(line)
            if complete:
                raise ValueError('records after stream end')
            kind = row['kind']
            if kind == 'subscription_market':
                assets = row['assets']
                market = row['market']
                if first or market in markets or len(assets) != 2 or len(set(assets.values())) != 2:
                    raise ValueError('invalid stream subscription')
                for outcome, asset in assets.items():
                    if asset in tokens:
                        raise ValueError('repeated token')
                    tokens[asset] = row['condition_id'], outcome
                markets[market] = row
                continue
            if kind == 'observation_end':
                complete = True
                continue
            received = datetime.fromisoformat(row['received_at'])
            if received.utcoffset() is None or (last and received < last):
                raise ValueError('unordered stream observations')
            if first is None:
                first = received
                next_cutoff = bisect_left(cutoffs, received)
            while next_cutoff < len(cutoffs) and cutoffs[next_cutoff] < received:
                frames[cutoffs[next_cutoff]] = latest.copy()
                next_cutoff += 1
            last = received
            count += 1
            event = row['event']
            expected_kind = 'book' if kind in {'initial_book', 'book_update'} else kind
            if event['event_type'] != expected_kind:
                raise ValueError('unexpected stream frame type')
            stamp = str(event['timestamp'])
            if not re.fullmatch(r'[0-9]{13}', stamp):
                raise ValueError('invalid stream source timestamp')
            millis = int(stamp)
            quoted = datetime.fromtimestamp(millis // 1000, timezone.utc) + timedelta(milliseconds=millis % 1000)
            if quoted > received:
                raise ValueError('future stream source timestamp')
            if kind in {'last_trade_price', 'tick_size_change'}:
                asset = event['asset_id']
                if tokens[asset][0] != event['market']:
                    raise ValueError('stream token does not match subscription')
                invalidate_depth(books, latest, asset)
            for entry, bid, ask in quote_entries(row):
                asset = entry['asset_id']
                if tokens[asset][0] != event['market']:
                    raise ValueError('stream token does not match subscription')
                if asset not in latest or quoted >= latest[asset][0]:
                    latest[asset] = quoted, received, bid, ask, book_depth(books, kind, entry, bid, ask)
                else:
                    invalidate_depth(books, latest, asset)
    summary = {'archive': path.name, 'events': count, 'complete': complete and not truncated,
               'first_received_at': first.isoformat() if first else None,
               'last_received_at': last.isoformat() if last else None}
    return markets, frames, summary


def stream_quote(binding, segments, cutoff, max_age_seconds):
    present = [(markets[binding['market']], frames[cutoff], summary) for markets, frames, summary in segments
               if binding['market'] in markets and cutoff in frames]
    if len(present) != 1:
        return {'status': 'ambiguous_stream_segments' if present else 'outside_stream_segment'}
    subscription, snapshot, summary = present[0]
    result = {'archive': summary['archive'], 'rules_sha256': subscription['rules_sha256']}
    if subscription['rules_sha256'] != binding['rules_sha256']:
        return result | {'status': 'different_rules'}
    books = []
    for outcome, token in subscription['assets'].items():
        if token not in snapshot:
            return result | {'status': 'missing_stream_side'}
        quoted, received, bid, ask, depth = snapshot[token]
        if cutoff - quoted > timedelta(seconds=max_age_seconds):
            return result | {'status': 'stale_stream_quote'}
        try:
            bid, ask = Decimal(bid), Decimal(ask)
            if not (bid.is_finite() and ask.is_finite() and 0 < bid < ask < 1):
                raise ValueError
        except (TypeError, ValueError, InvalidOperation):
            return result | {'status': 'invalid_stream_quote'}
        book = {'outcome': outcome, 'bid': str(bid), 'ask': str(ask),
                'quoted_at': quoted.isoformat(), 'received_at': received.isoformat()}
        if depth is not None:
            book.update(bid_size=str(depth[0]), ask_size=str(depth[1]))
        books.append(book)
    return result | {'status': 'observed_top_of_book', 'books': books}


def book_prices(rows):
    return {row['outcome']: (Decimal(row['bid']), Decimal(row['ask'])) for row in rows}


def compare_rest(stream, reference):
    if stream['status'] != 'observed_top_of_book' or reference['status'] != 'timed_depth':
        return
    stream['rest_comparison'] = ('same_prices' if book_prices(stream['books']) == book_prices(reference['books'])
                                 else 'different_prices')
    rest_by_outcome = {book['outcome']: book for book in reference['books']}
    depth_results = {}
    for book in stream['books']:
        rest = rest_by_outcome.get(book['outcome'])
        if ('bid_size' in book and rest and 'bid_size' in rest
                and datetime.fromisoformat(book['quoted_at']) == datetime.fromisoformat(rest['quoted_at'])):
            fields = ('bid', 'ask', 'bid_size', 'ask_size')
            depth_results[book['outcome']] = (
                'same_depth' if all(Decimal(book[key]) == Decimal(rest[key]) for key in fields) else 'different_depth'
            )
    if depth_results:
        stream['rest_depth_at_same_source_time'] = depth_results
