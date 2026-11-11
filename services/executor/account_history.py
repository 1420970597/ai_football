"""Bounded, read-only history scan; never renews sessions or submits an order."""
import asyncio
from datetime import datetime, timezone

from services.collector.leyu_protocol import LeyuError
from .account_session import AccountScope, VenueSession

FIELDS = frozenset({'order_no', 'identifier_kind', 'state', 'match_id', 'stake', 'payout',
                    'price', 'net_profit', 'pnl_basis', 'fees', 'currency', 'placed_at',
                    'settled_at', 'market_name', 'selection_name'})


async def scan(client, begin, end, *, max_pages=10):
    items, conflicts = {}, set()
    complete, error = True, ''
    for state in ('unsettled', 'settled'):
        seen = set()
        for page in range(1, max_pages + 1):
            try:
                result = await client.orders(state, page=page, size=100, begin=begin, end=end)
            except LeyuError as exc:
                return list(items.values()), False, exc.category
            if result.get('malformed_records') or result.get('conflicting_order_ids'):
                complete, error = False, 'history_invalid_records'
            for order_no in result.get('conflicting_order_ids', []):
                key = ('order', order_no)
                conflicts.add(key)
                items.pop(key, None)
            current = set()
            for raw in result['items']:
                key = (raw['identifier_kind'], raw['order_no'])
                current.add(key)
                try:
                    placed = datetime.fromisoformat(raw['placed_at'].replace('Z', '+00:00'))
                    if placed.tzinfo is None or not begin <= placed <= end:
                        raise ValueError()
                except (ValueError, TypeError, AttributeError):
                    complete, error = False, 'history_time_unverified'
                    continue
                item = {key: raw.get(key) for key in FIELDS}
                if key in conflicts:
                    continue
                previous = items.get(key)
                if previous and previous['state'] == state and previous != item:
                    conflicts.add(key)
                    del items[key]
                    complete, error = False, 'history_conflicting_records'
                elif not previous or state == 'settled':
                    items[key] = item
            if current.intersection(seen):
                complete, error = False, 'history_unstable_pagination'
            if not result['has_more']:
                break
            if not current or not current.difference(seen) or page == max_pages:
                complete, error = False, 'history_page_limit'
                break
            seen.update(current)
    return list(items.values()), complete, error


async def run(executor, work):
    result = {'attempt_id': work['attempt_id'], 'complete': False, 'items': [], 'error_category': ''}
    try:
        scope = AccountScope(**work['scope'])
        config = work['config']
        session = VenueSession(scope, config.get('leyu_api_base', ''), config.get('leyu_request_id', ''),
                               config.get('leyu_origin') or config.get('leyu_api_base', ''))
        begin, end = (datetime.fromisoformat(work['history'][key]) for key in ('begin', 'end'))
        async with asyncio.timeout(100):
            async with executor.client_factory(scope, session) as client:
                result['items'], result['complete'], result['error_category'] = await scan(client, begin, end)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        result['error_category'] = exc.category if isinstance(exc, LeyuError) else 'history_read_failed'
    result['observed_at'] = datetime.now(timezone.utc).isoformat()
    await executor.finish(f"/jobs/{work['job_id']}/history/finish", result)
