"""Export bounded immutable event evidence in a repeatable-read, read-only transaction."""

import asyncio
from datetime import datetime, timedelta
import json
import sys


QUERY = """
WITH signals AS MATERIALIZED (
    SELECT id, fixture_id, market_id, phase, market_kind, period, line, selection,
           score_basis, score_baseline, quote_time, recommendation_price, created_at
    FROM v2_expert_signals
    WHERE created_at >= $1 AND created_at < $2
)
SELECT s.id::text AS signal_id, s.fixture_id, f.sport, s.phase, s.market_kind, s.period,
       s.selection, s.created_at AS signal_at, s.recommendation_price AS signal_price,
       initial.source_updated_at AS source_at,
       initial.evidence->'market_snapshot' AS recommendation_book,
       attempts.rows AS preflights
FROM signals s
LEFT JOIN v2_fixtures f ON f.id = s.fixture_id
LEFT JOIN LATERAL (
    SELECT q.source_updated_at, q.evidence
    FROM v2_quote_evidence q
    WHERE q.evidence_kind = 'expert_signal' AND q.reference_id = s.id
      AND (q.fixture_id, q.market_id, q.period, q.line, q.selection, q.score_baseline)
          IS NOT DISTINCT FROM (s.fixture_id, s.market_id, s.period, s.line, s.selection, s.score_baseline)
      AND (q.source_updated_at, q.price) = (s.quote_time, s.recommendation_price)
      AND (q.evidence->>'score_basis', q.evidence->>'market_kind') = (s.score_basis, s.market_kind)
      AND q.captured_at = s.created_at
    ORDER BY q.id LIMIT 1
) initial ON true
LEFT JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_object(
        'captured_at', q.captured_at,
        'checked_at', q.evidence->>'provider_quote_checked_at',
        'price', q.price::text,
        'native_book', q.evidence->'native_market_prices'->'prices',
        'price_decision', q.evidence->'automatic_price_decision'->>'status',
        'reference_contract_missing', coalesce(
            (q.evidence->'automatic_price_decision'->'reasons') ? 'reference_contract_missing', false
        ),
        'native_accepted', o.provider_order_no IS NOT NULL
            AND o.provider_placed_at BETWEEN q.captured_at AND $3
    ) ORDER BY q.captured_at, q.id) AS rows
    FROM v2_bet_orders o
    JOIN v2_quote_evidence q ON q.reference_id = o.id AND q.evidence_kind = 'order_attempt'
       AND q.evidence->>'stage' = 'preflight_validated'
       AND q.evidence->>'contract' = 'order-stage-v1'
       AND q.evidence->>'signal_id' = s.id::text
       AND q.evidence->>'attempt_id' = o.attempt_id::text
    WHERE o.signal_id = s.id AND NOT o.manual
      AND (q.fixture_id, q.market_id, q.period, q.line, q.selection)
          IS NOT DISTINCT FROM (s.fixture_id, s.market_id, s.period, s.line, s.selection)
      AND (q.evidence->>'phase', q.evidence->>'market_kind', q.evidence->>'score_basis')
          IS NOT DISTINCT FROM (s.phase, s.market_kind, s.score_basis)
      AND (s.phase <> 'live' OR (
          jsonb_typeof(s.score_baseline->'home') = 'number'
          AND jsonb_typeof(s.score_baseline->'away') = 'number'
          AND q.score_baseline->'home' = s.score_baseline->'home'
          AND q.score_baseline->'away' = s.score_baseline->'away'
      ))
      AND q.captured_at >= s.created_at AND q.captured_at < $3
      AND (o.provider_placed_at IS NULL OR q.captured_at <= o.provider_placed_at)
) attempts ON true
ORDER BY s.created_at, s.id
"""


def utc(value):
    timestamp = datetime.fromisoformat(value)
    if timestamp.utcoffset() != timedelta(0):
        raise ValueError('UTC timestamps required')
    return timestamp


async def export(begin, end, cutoff):
    import asyncpg
    from app.config import database_url

    if not begin < end <= cutoff or end - begin > timedelta(hours=72) or cutoff - end > timedelta(days=7):
        raise ValueError('event cohort must have bounded UTC timestamps')
    conn = await asyncpg.connect(
        database_url(), timeout=5, command_timeout=10,
        server_settings={'TimeZone': 'UTC', 'default_transaction_read_only': 'on',
                         'statement_timeout': '8000', 'lock_timeout': '1000',
                         'application_name': 'research_event_evidence'},
    )
    try:
        async with conn.transaction(isolation='repeatable_read', readonly=True):
            observed = await conn.fetchval('SELECT transaction_timestamp()')
            if cutoff > observed:
                raise ValueError('event cohort cutoff is in the future')
            rows = []
            for record in await conn.fetch(QUERY, begin, end, cutoff):
                row = dict(record)
                for field in ('recommendation_book', 'preflights'):
                    if row[field] is not None:
                        row[field] = json.loads(row[field])
                rows.append(row)
            return {'schema': 1, 'begin': begin, 'end': end, 'cutoff': cutoff,
                    'observed_at': observed, 'rows': rows}
    finally:
        await conn.close()


if __name__ == '__main__':
    try:
        begin, end, cutoff = map(utc, sys.argv[1:])
        print(json.dumps(asyncio.run(export(begin, end, cutoff)), default=str))
    except Exception as error:
        raise SystemExit(f'event evidence export failed: {type(error).__name__}') from None
