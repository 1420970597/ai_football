"""Try one read-only native preflight and emit only sanitized paper evidence."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import sys

from services.collector.leyu_protocol import LeyuError
from services.executor.account_protocol import (
    AMOUNT_PATH, LATEST_MARKET_PATH, LIMITS_PATH, AccountProtocolClient, NativeSelection, number,
)
from services.executor.account_session import AccountScope, VenueSession


class PaperQuoteClient(AccountProtocolClient):
    async def _request(self, path, body=None, *, submission=False):
        if submission or path not in {AMOUNT_PATH, LATEST_MARKET_PATH, LIMITS_PATH}:
            raise LeyuError('paper_read_path_forbidden')
        return await super()._request(path, body)


async def inspect_offer(client, row, amount, observed_at):
    selection = NativeSelection.from_snapshot(row['native_selection'])
    prepared = await client.preflight(selection, amount, companion_options=row['companions'],
                                      require_better_odds=True)
    detail = prepared.payload['seriesOrders'][0]['orderDetailList'][0]
    price = number(detail['oddFinally'])
    prices = dict(prepared.market_prices)
    checked = datetime.fromisoformat(prepared.checked_at)
    observed = datetime.fromisoformat(observed_at)
    if (checked.utcoffset() != timedelta(0) or not observed <= checked <= observed + timedelta(seconds=15)
            or checked > datetime.now(timezone.utc)):
        return {'status': 'native_evidence_outside_cutoff'}
    if str(detail['playOptionsId']) != selection.selection_id:
        return {'status': 'native_selection_changed'}
    if (price <= 1 or prices.keys() != row['companions'].keys() | {selection.selection_code}
            or not all(number(value) > 1 for value in prices.values())
            or number(prices[selection.selection_code]) != price):
        return {'status': 'native_book_incomplete'}
    return {'status': 'native_preflight_available',
            'paper_amount_passing_native_limits': str(amount), 'native_quote_checked_at': prepared.checked_at,
            'native_selected_price': str(price), 'native_prices': prices,
            'native_upstream_quote_time': None, 'independent_price_evidence': 'not_assessed',
            'account_cost_evidence': 'not_assessed'}


async def capture(frame, client_factory=PaperQuoteClient):
    transport = frame.get('transport')
    rows = []
    sampled = False
    for candidate in frame['rows']:
        row = {key: value for key, value in candidate.items() if key not in {'native_selection', 'companions'}}
        if candidate['status'] == 'candidate':
            if not transport:
                row['status'] = 'paper_account_unavailable'
            elif sampled:
                row['status'] = 'paper_slot_budget'
            else:
                sampled = True
                scope = AccountScope(**transport['scope'])
                session = VenueSession(scope, transport['base_url'], transport['request_id'], transport['origin'])
                try:
                    async with asyncio.timeout(8), client_factory(scope, session, live_orders_enabled=False) as client:
                        row.update(await inspect_offer(client, candidate, Decimal(transport['amount']),
                                                       frame['observed_at']))
                except (LeyuError, TimeoutError) as exc:
                    row.update(status='native_preflight_unavailable',
                               reason=getattr(exc, 'category', 'paper_preflight_timeout'))
        rows.append(row)
    return {key: frame[key] for key in ('slot', 'begin', 'end', 'observed_at', 'capture_mode')} | {'rows': rows}


if __name__ == '__main__':
    try:
        print(json.dumps(asyncio.run(capture(json.load(sys.stdin))), sort_keys=True))
    except Exception as exc:
        raise SystemExit(f'paper capture failed: {type(exc).__name__}') from None
