"""Read a minute of natural recommendations independently of the order queue."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
import sys
from uuid import UUID

import asyncpg
from app.account_jobs import decode_document, scope
from app.config import load_settings
from execution_scope import NATIVE_SELECTION_CODES, SPORT_IDS, native_execution_market_supported, native_outcome_codes


CANDIDATES = """
SELECT s.id AS signal_id,s.fixture_id,s.expert_id,s.created_at,s.phase,s.market_kind,s.market_id,s.period,s.line,
       s.selection,s.score_basis,s.score_baseline::text,s.recommendation_price,s.quote_time,
       f.sport,f.phase AS current_phase,f.home_score,f.away_score,f.half_home_score,f.half_away_score,
       f.stale_projection_at,f.decision_context_only_at,
       m.posted_at,s.withdrawn_at,m.deleted_at,m.media_state,s.outcome,
       e.enabled AND src.enabled AND se.enabled AS current_trust,
       EXISTS (SELECT 1 FROM v2_expert_messages newer WHERE newer.source_id=m.source_id
             AND newer.platform_message_id=m.platform_message_id AND newer.revision>m.revision) AS superseded,
       initial.snapshot::text,live_market.options::text
FROM v2_expert_signals s
JOIN v2_fixtures f ON f.id=s.fixture_id
JOIN v2_expert_messages m ON m.id=s.message_id
JOIN v2_experts e ON e.id=s.expert_id
JOIN v2_expert_sources src ON src.id=m.source_id
LEFT JOIN v2_source_experts se ON se.source_id=m.source_id AND se.expert_id=s.expert_id
LEFT JOIN LATERAL (
    SELECT q.evidence->'market_snapshot' AS snapshot
    FROM v2_quote_evidence q
    WHERE q.evidence_kind='expert_signal' AND q.reference_id=s.id AND q.captured_at=s.created_at
      AND (q.fixture_id,q.market_id,q.period,q.selection)=(s.fixture_id,s.market_id,s.period,s.selection)
      AND (q.line IS NOT DISTINCT FROM s.line OR s.market_kind='winner'
           AND coalesce(q.line,0)=0 AND coalesce(s.line,0)=0)
      AND (q.price,q.source_updated_at)=(s.recommendation_price,s.quote_time)
      AND q.score_baseline IS NOT DISTINCT FROM s.score_baseline
      AND (q.evidence->>'score_basis',q.evidence->>'market_kind')=(s.score_basis,s.market_kind)
    ORDER BY q.id LIMIT 1
) initial ON true
LEFT JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_object(
       'selection',q.selection,'selection_id',q.selection_id,'price',q.price::text,
       'received_at',q.received_at,'source_updated_at',q.source_updated_at) ORDER BY q.selection) AS options
    FROM v2_current_quotes q
    WHERE (q.fixture_id,q.market_id,q.period,q.market_kind,q.score_basis)
        =(s.fixture_id,s.market_id,s.period,s.market_kind,s.score_basis)
      AND (q.line IS NOT DISTINCT FROM s.line OR s.market_kind='winner'
           AND coalesce(q.line,0)=0 AND coalesce(s.line,0)=0)
      AND (q.play_id,q.market_instance_id)=(initial.snapshot->>'play_id',initial.snapshot->>'market_instance_id')
      AND q.status='active' AND q.source_time_quality='known'
      AND q.price>1 AND q.price<'Infinity'::numeric AND nullif(q.selection_id,'') IS NOT NULL
      AND q.source_updated_at<=q.received_at AND q.received_at<=transaction_timestamp()
      AND q.source_updated_at>=transaction_timestamp()-interval '15 seconds'
) live_market ON true
WHERE s.created_at >= $1 AND s.created_at < $2
ORDER BY s.created_at,s.id
"""


def candidate(row, observed):
    baseline = json.loads(row['score_baseline'] or '{}')
    result = {key: row[key] for key in (
        'signal_id', 'fixture_id', 'expert_id', 'created_at', 'posted_at', 'sport', 'phase', 'market_kind',
        'market_id', 'period', 'line', 'selection', 'score_basis', 'recommendation_price', 'quote_time',
    )} | {'score_baseline': baseline}
    if (not row['current_trust'] or row['superseded'] or row['withdrawn_at'] or row['deleted_at']
            or row['media_state'] != 'ready'):
        return result | {'status': 'source_ineligible'}
    if not observed - timedelta(minutes=5) <= row['posted_at'] <= row['created_at'] <= observed:
        return result | {'status': 'source_deadline'}
    sport = SPORT_IDS.get(row['sport'])
    code = NATIVE_SELECTION_CODES.get(row['selection'])
    if (not sport or not row['fixture_id'].startswith('leyu:') or not row['fixture_id'][5:].isascii()
            or not row['fixture_id'][5:].isdecimal()
            or not native_execution_market_supported(sport, row['market_id'], row['market_kind'], row['period'])):
        return result | {'status': 'unsupported_contract'}
    expected = native_outcome_codes(row['market_kind'], sport)
    if code not in expected:
        return result | {'status': 'unsupported_contract'}
    if (row['phase'] != row['current_phase'] or row['outcome'] != 'pending'
            or row['stale_projection_at'] or row['decision_context_only_at']):
        return result | {'status': 'fixture_not_open'}
    scores = (row['home_score'], row['away_score'], row['half_home_score'], row['half_away_score'])
    if row['phase'] == 'live' and (
        any(type(value) is not int or value < 0 for value in scores[:2])
        or tuple(baseline.get(key) for key in ('home', 'away', 'half_home', 'half_away')) != scores
    ):
        return result | {'status': 'score_changed_or_unverified'}
    snapshot = json.loads(row['snapshot'] or '{}')
    if not snapshot.get('market_instance_id') or not snapshot.get('play_id'):
        return result | {'status': 'initial_market_missing'}
    options = json.loads(row['options'] or '[]')
    options_by_code = {NATIVE_SELECTION_CODES.get(option['selection']): option for option in options}
    if (len(options) != len(expected) or options_by_code.keys() != expected
            or len({option['selection_id'] for option in options}) != len(expected)
            or len({(option['received_at'], option['source_updated_at']) for option in options}) != 1):
        return result | {'status': 'current_book_missing_or_incomplete'}
    selected = options_by_code[code]
    selection = {
        'match_id': row['fixture_id'].removeprefix('leyu:'),
        'market_instance_id': snapshot['market_instance_id'], 'play_id': snapshot['play_id'],
        'selection_id': selected['selection_id'], 'selection_code': code, 'sport_id': sport,
        'chpid': row['market_id'], 'kind': row['market_kind'], 'period': row['period'],
        'line': row['line'], 'price': selected['price'], 'phase': row['phase'],
        'score_benchmark': f'{scores[0]}:{scores[1]}' if row['phase'] == 'live' else '',
    }
    return result | {
        'status': 'candidate', 'projection_received_at': selected['received_at'],
        'projection_source_at': selected['source_updated_at'],
        'projection_prices': {key: option['price'] for key, option in options_by_code.items()},
        'native_selection': selection,
        'companions': {key: option['selection_id'] for key, option in options_by_code.items() if key != code},
    }


async def read_slot(anchor, *, projection_only=False):
    settings = load_settings()
    conn = await asyncpg.connect(settings.database_url, timeout=5, command_timeout=8,
                                 server_settings={'default_transaction_read_only': 'on', 'TimeZone': 'UTC',
                                                  'statement_timeout': '6500', 'lock_timeout': '500'})
    try:
        async with conn.transaction(isolation='repeatable_read', readonly=True):
            observed = await conn.fetchval('SELECT transaction_timestamp()')
            if anchor.utcoffset() != timedelta(0) or anchor.second or anchor.microsecond:
                raise ValueError('utc_minute_required')
            if not timedelta(0) <= observed - anchor < timedelta(seconds=30):
                raise ValueError('slot_observation_missed')
            begin, end = anchor - timedelta(seconds=90), anchor - timedelta(seconds=30)
            rows = [candidate(row, observed) for row in await conn.fetch(CANDIDATES, begin, end)]
            frame = {'slot': anchor, 'begin': begin, 'end': end, 'observed_at': observed, 'rows': rows,
                     'capture_mode': 'projection_only' if projection_only else 'paper_preflight'}
            if projection_only or not any(row['status'] == 'candidate' for row in rows):
                return frame
            binding = os.environ.get('AUTOMATIC_PRICE_ACCOUNT')
            if not binding:
                return frame
            account = await conn.fetchrow("""
                SELECT a.id,a.user_id,a.config_revision,a.encrypted_session,a.provider_identity_hash,b.stake_per_order
                FROM v2_leyu_accounts a JOIN v2_user_bet_settings b ON b.user_id=a.user_id
                WHERE a.id=$1 AND a.health_state='healthy' AND a.renewal_state='ready'
                  AND a.encrypted_session IS NOT NULL AND a.last_verified_at>=now()-interval '120 seconds'
                  AND a.id=b.consent_account_id AND a.binding_revision=b.consent_binding_revision
                  AND NOT b.emergency_stopped AND b.stake_per_order>=0.01 AND b.stake_per_order<=b.max_per_order
                  AND EXISTS (SELECT 1 FROM v2_bet_orders o WHERE o.account_id=a.id
                      AND o.provider_identity_hash=a.provider_identity_hash AND o.provider_order_no IS NOT NULL)
            """, UUID(binding))
            if account is None:
                return frame
            amount = Decimal(account['stake_per_order'])
            if not amount.is_finite() or amount != amount.quantize(Decimal('0.01')):
                return frame
            session = decode_document(settings.encryption_key, account['encrypted_session'])
            base = session['leyu_api_base']
            account_binding = sha256(
                f"paper-account-v1\0{account['id']}\0{account['provider_identity_hash']}".encode(),
            ).hexdigest()
            return frame | {'transport': {'scope': scope(account), 'base_url': base,
                                          'request_id': session['leyu_request_id'],
                                          'paper_account_binding': account_binding,
                                          'origin': session.get('leyu_origin') or base, 'amount': str(amount)}}
    finally:
        await conn.close()


if __name__ == '__main__':
    try:
        if len(sys.argv) not in (2, 3) or len(sys.argv) == 3 and sys.argv[2] != '--projection-only':
            raise ValueError('invalid_paper_capture_arguments')
        slot = (datetime.now(timezone.utc).replace(second=0, microsecond=0) if sys.argv[1] == 'now'
                else datetime.fromisoformat(sys.argv[1]))
        print(json.dumps(asyncio.run(read_slot(slot, projection_only=len(sys.argv) == 3)), default=str))
    except Exception as exc:
        raise SystemExit(f'paper discovery failed: {getattr(exc, "category", type(exc).__name__)}') from None
