"""Auditable statistics cycles; original signals, settlements and orders are never rewritten."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import uuid

import asyncpg

from .auth import audit
from .expert_profitability import PROFITABILITY_DAYS, profitability_rows
from .expert_statistics import StatisticsScope, signal_scope

RESET_LOCK = 630030
MAX_RESET_SIGNALS = 100000


async def preview_reset(conn, *, at: datetime | None = None) -> dict:
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError('reset preview requires timezone-aware UTC time')
    at = at.astimezone(timezone.utc)
    rows = await profitability_rows(conn, at=at)
    affected = sorted((r for r in rows if r['effective_settled_signals'] > 0
                       and Decimal(str(r['unit_net'])).is_finite()
                       and Decimal(str(r['unit_net'])) <= 0), key=lambda r: str(r['expert_id']))
    experts = [{'expert_id': str(r['expert_id']), 'effective_settled_signals': r['effective_settled_signals'],
                'unit_net': str(r['unit_net'])} for r in affected]
    prefix, args = signal_scope(StatisticsScope(datetime.min.replace(tzinfo=timezone.utc), at))
    signals = await conn.fetch(prefix +
        "SELECT id,expert_id,outcome,unit_net,result_version FROM scoped WHERE statistics_eligible "
        "AND outcome IN('win','half_win','loss','half_loss') AND expert_id=ANY($8::uuid[]) "
        "ORDER BY id LIMIT $9", *args, [r['expert_id'] for r in affected], MAX_RESET_SIGNALS + 1)
    if len(signals) > MAX_RESET_SIGNALS:
        raise ValueError('reset snapshot exceeds reviewed batch budget; no partial reset allowed')
    snapshot = [{'signal_id': str(r['id']), 'expert_id': str(r['expert_id']), 'outcome': r['outcome'],
                 'unit_net': str(r['unit_net']), 'result_version': r['result_version']} for r in signals]
    plan = {'as_of': at.isoformat(), 'eligibility_days': PROFITABILITY_DAYS,
            'experts': experts, 'signals': snapshot}
    plan['fingerprint'] = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return plan


async def apply_reset(pool, *, idempotency_key: str, expected_fingerprint: str,
                      as_of: datetime, reason: str, actor_id=None) -> dict:
    for attempt in range(3):
        try:
            return await _apply_reset(pool, idempotency_key=idempotency_key,
                expected_fingerprint=expected_fingerprint, as_of=as_of, reason=reason, actor_id=actor_id)
        except (asyncpg.SerializationError, asyncpg.UniqueViolationError):
            # Repeatable-read takes its snapshot before an advisory lock wait.
            # Reopen after a concurrent commit so the same key sees its batch.
            if attempt == 2:
                raise


async def _apply_reset(pool, *, idempotency_key: str, expected_fingerprint: str,
                       as_of: datetime, reason: str, actor_id=None) -> dict:
    if not 1 <= len(idempotency_key) <= 120 or not 1 <= len(reason.strip()) <= 500:
        raise ValueError('reset requires a bounded idempotency key and reason')
    async with pool.acquire() as conn:
        # A concurrent settlement changes the plan or raises serialization failure;
        # the caller must review a fresh preview instead of silently clearing more data.
        async with conn.transaction(isolation='repeatable_read'):
            await conn.execute('SELECT pg_advisory_xact_lock($1)', RESET_LOCK)
            previous = await conn.fetchrow('SELECT * FROM v2_expert_statistics_resets WHERE idempotency_key=$1', idempotency_key)
            if previous:
                if previous['before_summary']['fingerprint'] != expected_fingerprint:
                    raise ValueError('idempotency key already belongs to a different reset preview')
                return {'reset_id': str(previous['id']), 'idempotent': True,
                        'expert_count': len(previous['before_summary']['experts']),
                        'signal_count': len(previous['before_summary']['signals'])}
            plan = await preview_reset(conn, at=as_of)
            if plan['fingerprint'] != expected_fingerprint:
                raise ValueError('statistics changed after preview; review a fresh snapshot before resetting')
            signal_ids = [uuid.UUID(s['signal_id']) for s in plan['signals']]
            await conn.fetch('SELECT id FROM v2_expert_signals WHERE id=ANY($1::uuid[]) ORDER BY id FOR SHARE', signal_ids)
            reset_id = uuid.uuid4()
            await conn.execute('INSERT INTO v2_expert_statistics_resets(id,idempotency_key,created_at,actor_id,reason,before_summary) '
                               'VALUES($1,$2,now(),$3,$4,$5)', reset_id, idempotency_key, actor_id, reason.strip(), plan)
            await conn.executemany('INSERT INTO v2_expert_statistics_reset_experts(reset_id,expert_id) VALUES($1,$2)',
                                   [(reset_id, uuid.UUID(e['expert_id'])) for e in plan['experts']])
            await conn.executemany('INSERT INTO v2_expert_statistics_reset_signals(reset_id,signal_id) VALUES($1,$2)',
                                   [(reset_id, s) for s in signal_ids])
            result = {'reset_id': str(reset_id), 'idempotent': False,
                      'expert_count': len(plan['experts']), 'signal_count': len(signal_ids),
                      'fingerprint': plan['fingerprint']}
            await audit(conn, actor_id, 'admin' if actor_id else 'system', 'expert_statistics_reset',
                        'expert_statistics_cycle', str(reset_id), details=result | {'reason': reason.strip()})
            return result


async def revoke_reset(pool, *, reset_id: uuid.UUID, reason: str, actor_id=None) -> dict:
    if not 1 <= len(reason.strip()) <= 500:
        raise ValueError('statistics reset revocation requires a bounded reason')
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock($1)', RESET_LOCK)
            batch = await conn.fetchrow('SELECT * FROM v2_expert_statistics_resets WHERE id=$1 FOR UPDATE', reset_id)
            if not batch:
                raise ValueError('statistics reset does not exist')
            if batch['revoked_at']:
                return {'reset_id': str(reset_id), 'idempotent': True}
            await conn.execute('UPDATE v2_expert_statistics_resets SET revoked_at=now(),revocation_reason=$2 WHERE id=$1',
                               reset_id, reason.strip())
            await audit(conn, actor_id, 'admin' if actor_id else 'system', 'expert_statistics_reset_revoked',
                        'expert_statistics_cycle', str(reset_id), details={'reason': reason.strip()})
            return {'reset_id': str(reset_id), 'idempotent': False}
