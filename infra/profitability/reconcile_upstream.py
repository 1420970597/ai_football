"""Read a scoped settled-order history and print aggregate reconciliation evidence."""

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import sys

from services.collector.leyu_protocol import LeyuError
from services.executor.account_protocol import AccountProtocolClient
from services.executor.account_session import AccountScope, VenueSession


def instant(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('aware time required')
    return parsed.astimezone(timezone.utc)


def index_orders(rows):
    by_order = {}
    for row in rows:
        order_no = row['order_no']
        if not order_no or order_no in by_order:
            raise ValueError('duplicate or missing order identity')
        by_order[order_no] = row
    return by_order


def reconcile_window(page, local, begin, end):
    expected = index_orders(row for row in local if begin <= instant(row['booked_at']) < end)
    orders = [row for row in page['items'] if row['identifier_kind'] == 'order' and row['state'] == 'settled']
    upstream = index_orders(orders)
    counts = Counter()
    for number, order in upstream.items():
        at = instant(order['settled_at']) if order['settled_at'] else None
        if at is None:
            counts['missing_upstream_settlement_time'] += 1
        elif not begin <= at < end:
            counts['outside_settlement_window'] += 1
        if order['placed_at'] and instant(order['placed_at']) < begin:
            counts['placed_before_window'] += 1
        if order['currency'] is None:
            counts['missing_currency'] += 1
        if order['fees'] is None:
            counts['missing_fees'] += 1
        if number not in expected:
            continue
        prior = expected[number]
        if at is not None and prior['settled_at'] is None:
            counts['recoverable_local_settlement_time'] += 1
        elif at is not None and at != instant(prior['settled_at']):
            counts['settlement_time_difference'] += 1
        for key in ('stake', 'payout', 'net_profit'):
            left, right = order[key], prior[key]
            if left is None or right is None:
                counts[f'{key}_missing'] += 1
            elif Decimal(left) != Decimal(right):
                counts[f'{key}_difference'] += 1
    missing = expected.keys() - upstream.keys()
    extra = upstream.keys() - expected.keys()
    complete_page = (not page['has_more'] and not page['malformed_records'] and
                     page['total'] == len(orders) == len(page['items']))
    return {
        'begin': begin.isoformat(), 'end': end.isoformat(), 'complete_page': complete_page,
        'local_settled': len(expected), 'upstream_settled': len(upstream),
        'matched': len(expected.keys() & upstream.keys()), 'local_only': len(missing),
        'upstream_only': len(extra), 'issues': dict(counts),
    }


async def collect(client, start, local):
    windows = []
    for ordinal in range(4):
        begin = start + timedelta(hours=ordinal * 12)
        end = begin + timedelta(hours=12)
        try:
            page = await client.orders('settled', begin=begin, end=end)
        except LeyuError as exc:
            windows.append({'begin': begin.isoformat(), 'end': end.isoformat(), 'error_category': exc.category})
            break
        windows.append(reconcile_window(page, local, begin, end))
        if ordinal < 3:
            await asyncio.sleep(1.2)
    return windows


async def main():
    payload = json.load(sys.stdin)
    start = instant(payload['start'])
    if start + timedelta(hours=48) > datetime.now(timezone.utc):
        raise ValueError('only complete windows can be sampled')
    scope = AccountScope(**payload['scope'])
    session = VenueSession(scope, payload['base_url'], payload['request_id'], payload['origin'])
    async with asyncio.timeout(55), AccountProtocolClient(scope, session) as client:
        return await collect(client, start, payload['local'])


if __name__ == '__main__':
    try:
        print(json.dumps(asyncio.run(main()), sort_keys=True))
    except Exception as exc:
        raise SystemExit(getattr(exc, 'category', type(exc).__name__)) from None
