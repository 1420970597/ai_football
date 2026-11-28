"""Export a fixed cohort of accepted orders and immutable evidence for offline research."""

import asyncio
from datetime import datetime, timedelta
import json
import sys

import asyncpg
from app.config import database_url


QUERY = """
WITH fills AS MATERIALIZED (
    SELECT o.* FROM v2_bet_orders o
    WHERE NOT o.manual AND o.provider_order_no IS NOT NULL
      AND coalesce(o.provider_placed_at,o.created_at) >= $1
      AND coalesce(o.provider_placed_at,o.created_at) < $2
)
SELECT o.id AS order_id,s.fixture_id,s.expert_id,f.sport,s.market_kind,
       o.provider_placed_at AS placed_at,s.created_at AS signal_at,
       s.recommendation_price AS signal_price,p.captured_at AS preflight_at,
       p.price AS preflight_price,p.evidence->'native_market_prices'->>'overround' AS overround,
       coalesce(r.receipts,'[]'::jsonb) AS receipts
FROM fills o
LEFT JOIN v2_expert_signals s ON s.id=o.signal_id
LEFT JOIN v2_fixtures f ON f.id=s.fixture_id
LEFT JOIN LATERAL (
    SELECT q.captured_at,q.price,q.evidence FROM v2_quote_evidence q
    WHERE q.reference_id=o.id AND q.evidence_kind='order_attempt'
      AND q.evidence->>'stage'='preflight_validated'
      AND q.evidence->>'attempt_id'=o.attempt_id::text AND q.evidence->>'signal_id'=s.id::text
      AND (q.fixture_id,q.market_id,q.period,q.selection,q.line)
          IS NOT DISTINCT FROM (s.fixture_id,s.market_id,s.period,s.selection,s.line)
      AND (q.evidence->>'phase',q.evidence->>'market_kind')=(s.phase,s.market_kind)
      AND q.captured_at BETWEEN s.created_at AND o.provider_placed_at
    ORDER BY q.captured_at DESC,q.id DESC LIMIT 1
) p ON true
LEFT JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_object(
        'observed_at',e.observed_at,'settled_at',e.payload->>'settled_at','stake',e.payload->>'stake',
        'payout',e.payload->>'payout','fees',e.payload->>'fees','currency',e.payload->>'currency',
        'net',e.payload->>'net_profit') ORDER BY e.observed_at,e.id) AS receipts
    FROM v2_order_receipt_evidence e
    WHERE e.order_id=o.id AND e.payload->>'order_no'=o.provider_order_no AND e.payload->>'state'='settled'
      AND e.observed_at <= $3
) r ON true
ORDER BY o.provider_placed_at,o.id
"""


def utc(value):
    moment = datetime.fromisoformat(value)
    if moment.utcoffset() != timedelta(0):
        raise ValueError('UTC timestamps required')
    return moment


async def export(begin, end, cutoff):
    conn = await asyncpg.connect(database_url(), timeout=5, command_timeout=15,
                                 server_settings={'default_transaction_read_only': 'on',
                                                  'statement_timeout': '12000', 'TimeZone': 'UTC'})
    try:
        async with conn.transaction(isolation='repeatable_read', readonly=True):
            now = await conn.fetchval('SELECT transaction_timestamp()')
            if not begin < end <= cutoff <= now or cutoff - begin > timedelta(days=120):
                raise ValueError('invalid cohort bounds')
            valid_binding = await conn.fetchval("""
                SELECT count(DISTINCT (account_id,provider_identity_hash))=1
                    AND count(*) FILTER (WHERE account_id IS NULL OR provider_identity_hash IS NULL)=0
                    AND count(*)=count(DISTINCT provider_order_no)
                FROM v2_bet_orders WHERE NOT manual AND provider_order_no IS NOT NULL
                  AND coalesce(provider_placed_at,created_at)>=$1 AND coalesce(provider_placed_at,created_at)<$2
            """, begin, end)
            if not valid_binding:
                raise ValueError('cohort requires one verified ledger binding and unique native orders')
            rows = await conn.fetch(QUERY, begin, end, cutoff)
            return {'begin': begin.isoformat(), 'end': end.isoformat(), 'cutoff': cutoff.isoformat(),
                    'rows': [dict(row) | {'receipts': json.loads(row['receipts'])} for row in rows]}
    finally:
        await conn.close()


if __name__ == '__main__':
    try:
        print(json.dumps(asyncio.run(export(*(utc(value) for value in sys.argv[1:]))), default=str))
    except (asyncpg.PostgresError, OSError, ValueError) as exc:
        raise SystemExit(f'simulation export failed: {type(exc).__name__}: {exc if type(exc) is ValueError else ""}') from None
