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


PAGE_LIMIT = 3
REQUEST_INTERVAL = 1.2


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
    bounded_without_upstream_time = 0
    currencies = {order['currency'] for order in orders if order['currency'] is not None}
    if len(currencies) > 1:
        counts['mixed_upstream_currencies'] = len(currencies)
    for number, order in upstream.items():
        at = instant(order['settled_at']) if order['settled_at'] else None
        if at is None:
            counts['missing_upstream_settlement_time'] += 1
        elif not begin <= at < end:
            counts['outside_settlement_window'] += 1
        placed = instant(order['placed_at']) if order['placed_at'] else None
        if placed is not None and placed < begin:
            counts['placed_before_window'] += 1
        if order['currency'] is None:
            counts['missing_currency'] += 1
        if order['fees'] is None:
            counts['missing_fees'] += 1
        if number not in expected:
            continue
        prior = expected[number]
        if at is None and prior['settled_at'] is None and placed is not None:
            local_placement = prior.get('placed_at')
            first_settled = prior.get('first_settled_observed_at')
            if (local_placement and first_settled and instant(local_placement) == placed
                    and begin <= placed <= instant(first_settled) < end):
                bounded_without_upstream_time += 1
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
        for key in ('currency', 'fees'):
            left, right = order[key], prior.get(key)
            if left is None:
                continue
            if right is None:
                counts[f'recoverable_local_{key}'] += 1
            elif (left != right if key == 'currency' else Decimal(left) != Decimal(right)):
                counts[f'{key}_difference'] += 1
    missing = expected.keys() - upstream.keys()
    extra = upstream.keys() - expected.keys()
    complete_page = (not page['has_more'] and not page['malformed_records'] and
                     page['total'] == len(orders) == len(page['items']) and not page.get('scan_issue'))
    matched_complete_page = complete_page and bool(orders) and not (missing or extra)
    return {
        'begin': begin.isoformat(), 'end': end.isoformat(), 'complete_page': complete_page,
        'settlement_window_attribution_complete': matched_complete_page
            and not counts['outside_settlement_window']
            and counts['missing_upstream_settlement_time'] == bounded_without_upstream_time,
        'bounded_without_upstream_time': bounded_without_upstream_time,
        'order_receipt_fields_reconciled': matched_complete_page and not (counts.keys() - {'placed_before_window'}),
        'local_settled': len(expected), 'upstream_settled': len(upstream),
        'matched': len(expected.keys() & upstream.keys()), 'local_only': len(missing),
        'upstream_only': len(extra), 'issues': dict(counts),
    }


async def scan_window(client, begin, end):
    result = {'items': [], 'total': None, 'malformed_records': 0, 'has_more': True,
              'pages_read': 0, 'scan_issue': None}
    seen = set()
    for number in range(1, PAGE_LIMIT + 1):
        if number > 1:
            await asyncio.sleep(REQUEST_INTERVAL)
        try:
            page = await client.orders('settled', page=number, begin=begin, end=end)
        except LeyuError as exc:
            result['scan_issue'] = exc.category
            break
        result['pages_read'] += 1
        if number == 1:
            result['total'] = page['total']
        if page['total'] != result['total']:
            result['scan_issue'] = 'upstream_total_changed'
            break
        result['has_more'] = page['has_more']
        result['malformed_records'] += page['malformed_records']
        for item in page['items']:
            identity = item['identifier_kind'], item['order_no']
            if identity in seen:
                result['scan_issue'] = 'duplicate_upstream_identity'
                continue
            seen.add(identity)
            result['items'].append(item)
        if result['scan_issue'] or page['malformed_records'] or not page['has_more']:
            break
        if not page['items']:
            result['scan_issue'] = 'empty_page_with_more'
            break
    if result['has_more'] and result['pages_read'] == PAGE_LIMIT and result['scan_issue'] is None:
        result['scan_issue'] = 'upstream_page_limit'
    return result


async def collect(client, start, local, window_count=4):
    windows = []
    for ordinal in range(window_count):
        begin = start + timedelta(hours=ordinal * 12)
        end = begin + timedelta(hours=12)
        scan = await scan_window(client, begin, end)
        result = reconcile_window(scan, local, begin, end)
        result.update(pages_read=scan['pages_read'], scan_issue=scan['scan_issue'])
        windows.append(result)
        if scan['pages_read'] == 0:
            break
        if ordinal + 1 < window_count:
            await asyncio.sleep(REQUEST_INTERVAL)
    return windows


async def main():
    payload = json.load(sys.stdin)
    start = instant(payload['start'])
    window_count = payload.get('windows', 4)
    if type(window_count) is not int or not 1 <= window_count <= 4:
        raise ValueError('one to four windows required')
    if start + timedelta(hours=window_count * 12) > datetime.now(timezone.utc):
        raise ValueError('only complete windows can be sampled')
    scope = AccountScope(**payload['scope'])
    session = VenueSession(scope, payload['base_url'], payload['request_id'], payload['origin'])
    async with asyncio.timeout(41 * window_count), AccountProtocolClient(scope, session) as client:
        return await collect(client, start, payload['local'], window_count)


if __name__ == '__main__':
    try:
        print(json.dumps(asyncio.run(main()), sort_keys=True))
    except Exception as exc:
        raise SystemExit(getattr(exc, 'category', type(exc).__name__)) from None
