"""Attach contemporaneous independent price decisions to paper preflights."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
import sys
from types import SimpleNamespace

from app.order_price import decision


async def assess(row, socket, inquire=decision, now=None):
    cutoff = now or datetime.now(timezone.utc)
    checked = datetime.fromisoformat(row['native_quote_checked_at'])
    if not socket:
        return {'status': 'reference_socket_unconfigured'}
    if not timedelta(0) <= cutoff - checked <= timedelta(seconds=5):
        return {'status': 'reference_cutoff_expired'}
    baseline = row['score_baseline']
    signal = row | {'current_kind': row['market_kind'], 'home_score': baseline.get('home'),
                    'away_score': baseline.get('away')}
    result = await inquire(SimpleNamespace(automatic_price_socket=socket), signal,
                           Decimal(row['native_selected_price']), checked, cutoff)
    return result | {'evaluated_at': cutoff.isoformat()}


async def annotate(frame, socket):
    for row in frame['rows']:
        if row['status'] == 'native_preflight_available':
            row['independent_price_evidence'] = await assess(row, socket)
    return frame


if __name__ == '__main__':
    try:
        print(json.dumps(asyncio.run(annotate(json.load(sys.stdin), os.environ.get('AUTOMATIC_PRICE_SOCKET'))),
                         sort_keys=True))
    except Exception as exc:
        raise SystemExit(f'paper reference assessment failed: {type(exc).__name__}') from None
