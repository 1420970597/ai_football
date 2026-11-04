"""可追溯赛果版本与专家收益；不修改或冒充真实订单结算。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from .expert_statistics import QUOTE_VALID_SQL
from .settlement import pending, settle
from .handicap_lines import line_basis

ALGORITHM_VERSION = 'football-asian-4-native-line-basis'


def result_facts(row):
    facts = {'status': row['status'], 'home': row['home_score'], 'away': row['away_score'],
            'half_home': row['half_home_score'], 'half_away': row['half_away_score'],
            'provider_reference': row['provider_reference'], 'source_updated_at': row['source_updated_at'].isoformat() if row['source_updated_at'] else None,
            'evidence': row['evidence']}
    if row['source_updated_at'] is None:
        facts.update(observed_at=row['observed_at'].isoformat(), source_time_quality='unknown')
    return facts


async def project_settlements(conn, fixture, version, facts):
    rows = await conn.fetch('SELECT s.*,' + QUOTE_VALID_SQL +
                            ' AS quote_valid FROM v2_expert_signals s WHERE fixture_id=$1 ORDER BY id FOR UPDATE', fixture['id'])
    projections = []
    for row in rows:
        inputs = {'sport': fixture['sport'], 'kind': row['market_kind'], 'period': row['period'],
                  'phase': row['phase'], 'price': str(row['recommendation_price']), 'line': str(row['line']) if row['line'] is not None else None,
                  'selection': row['selection'], 'score_basis': row['score_basis'], 'score_baseline': row['score_baseline'],
                  'line_basis': line_basis(row, fixture['id'])}
        value = settle(inputs, facts) if row['quote_valid'] else pending('recommendation_quote_evidence_missing_or_mismatched')
        calculation = {'signal': inputs, 'result': facts}
        projections.append((row['id'], value, calculation))
    if projections:
        await _store_settlement_batch(conn, fixture['id'], version, projections)
    return {'settled': sum(value.outcome != 'pending' for _, value, _ in projections), 'signals': len(rows)}


async def _store_settlement_batch(conn, fixture_id, version, projections):
    """Keep the caller's fixture/signal locks while bounding remote round trips."""
    proposed = [{'signal_id': str(signal_id), 'outcome': value.outcome,
                 'unit_net': str(value.unit_net) if value.unit_net is not None else None,
                 'effective_stake': str(value.effective_stake), 'reason': value.reason, 'calculation': calculation}
                for signal_id, value, calculation in projections]
    await conn.execute(
        'WITH proposed AS (SELECT * FROM jsonb_to_recordset($4::jsonb) AS p('
        'signal_id uuid,outcome text,unit_net numeric,effective_stake numeric,reason text,calculation jsonb)) '
        'INSERT INTO v2_signal_settlements(signal_id,fixture_id,result_version,algorithm_version,outcome,unit_net,effective_stake,reason,calculation,created_at) '
        'SELECT p.signal_id,$1,$2,$3,p.outcome,p.unit_net,p.effective_stake,p.reason,p.calculation,'
        'COALESCE((SELECT min(old.created_at) FROM v2_signal_settlements old '
        'WHERE old.signal_id=p.signal_id AND old.result_version=$2 AND old.outcome=p.outcome '
        'AND old.unit_net IS NOT DISTINCT FROM p.unit_net AND old.effective_stake=p.effective_stake '
        'AND old.reason=p.reason),now()) FROM proposed p ORDER BY p.signal_id '
        'ON CONFLICT(signal_id,result_version,algorithm_version) DO NOTHING',
        fixture_id, version, ALGORITHM_VERSION, proposed)
    recorded = await conn.fetch(
        'SELECT * FROM v2_signal_settlements WHERE signal_id=ANY($1::uuid[]) '
        'AND result_version=$2 AND algorithm_version=$3 ORDER BY signal_id',
        [signal_id for signal_id, _, _ in projections], version, ALGORITHM_VERSION)
    by_signal = {row['signal_id']: row for row in recorded}
    for signal_id, value, calculation in projections:
        row = by_signal.get(signal_id)
        expected = (value.outcome, value.unit_net, value.effective_stake, value.reason, calculation)
        actual = tuple(row[key] for key in ('outcome', 'unit_net', 'effective_stake', 'reason', 'calculation')) if row else None
        if actual != expected:
            raise HTTPException(409, '不可变结算证据与重算不一致，必须保留原记录并核对算法版本')
    # Validate the entire immutable batch before changing any signal projection.
    # Replaying the same result must not move its return to a new day.
    updated = [{'signal_id': str(row['signal_id']), 'outcome': row['outcome'],
                'unit_net': str(row['unit_net']) if row['unit_net'] is not None else None,
                'effective_stake': str(row['effective_stake']), 'reason': row['reason'],
                'created_at': row['created_at'].isoformat()} for row in recorded]
    await conn.execute(
        "UPDATE v2_expert_signals s SET outcome=p.outcome,unit_net=p.unit_net,effective_stake=p.effective_stake,"
        "settlement_reason=p.reason,result_version=$2,settled_at=CASE WHEN p.outcome='pending' THEN NULL ELSE p.created_at END "
        "FROM jsonb_to_recordset($1::jsonb) AS p(signal_id uuid,outcome text,unit_net numeric,"
        "effective_stake numeric,reason text,created_at timestamptz) WHERE s.id=p.signal_id", updated, version)


async def rebuild_settlements(conn, fixture_id):
    fixture = await conn.fetchrow('SELECT id,sport FROM v2_fixtures WHERE id=$1 FOR UPDATE', fixture_id)
    if not fixture:
        raise HTTPException(404, '赛果对应比赛不存在')
    latest = await conn.fetchrow('SELECT * FROM v2_fixture_results WHERE fixture_id=$1 ORDER BY version DESC LIMIT 1', fixture_id)
    if not latest:
        raise HTTPException(409, '没有可重建的来源赛果证据')
    projected = await project_settlements(conn, fixture, latest['version'], result_facts(latest))
    return {'version': latest['version'], 'algorithm_version': ALGORITHM_VERSION, **projected}


async def ingest_result(conn, item) -> dict:
    observed_at = getattr(item, 'observed_at', None)
    source_time = item.source_updated_at
    clock = source_time or observed_at
    if clock is None or clock > datetime.now(timezone.utc) + timedelta(seconds=5):
        raise HTTPException(422, '赛果源时间晚于当前时间')
    fixture = await conn.fetchrow('SELECT id,sport FROM v2_fixtures WHERE id=$1 FOR UPDATE', item.fixture_id)
    if not fixture:
        raise HTTPException(404, '赛果对应比赛不存在')
    previous = await conn.fetchrow('SELECT * FROM v2_fixture_results WHERE fixture_id=$1 ORDER BY version DESC LIMIT 1', item.fixture_id)
    facts = {'status': item.status, 'home': item.home, 'away': item.away, 'half_home': item.half_home,
             'half_away': item.half_away, 'provider_reference': item.provider_reference,
             'source_updated_at': source_time.astimezone(timezone.utc).isoformat() if source_time else None, 'evidence': item.evidence}
    # Observation receipts and full-response hashes change at each poll; only a
    # changed result creates another result version. Keep the original receipt.
    digest_facts = facts if source_time else {k:v for k,v in facts.items() if k != 'evidence'}
    digest = hashlib.sha256(json.dumps(digest_facts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if not source_time:
        facts.update(observed_at=observed_at.astimezone(timezone.utc).isoformat(), source_time_quality='unknown')
    if previous and previous['content_hash'] == digest:
        return {'version': previous['version'], 'duplicate': True,
                **await project_settlements(conn, fixture, previous['version'], result_facts(previous))}
    if previous and not source_time and previous['source_updated_at']:
        raise HTTPException(409, '未知源修订时钟的详情不能覆盖已有源修订证据')
    if previous and clock <= (previous['source_updated_at'] or previous['observed_at']):
        raise HTTPException(409, '旧赛果或同时间冲突赛果不能覆盖当前版本')
    version = previous['version'] + 1 if previous else 1
    await conn.execute(
        'INSERT INTO v2_fixture_results(fixture_id,version,provider_reference,content_hash,status,home_score,away_score,'
        'half_home_score,half_away_score,source_updated_at,evidence,observed_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)',
        item.fixture_id, version, item.provider_reference, digest, item.status, item.home, item.away,
        item.half_home, item.half_away, source_time, item.evidence, observed_at)
    if item.status == 'confirmed':
        await conn.execute("UPDATE v2_fixtures SET phase='finished',match_state='finished',home_score=$2,away_score=$3,half_home_score=$4,half_away_score=$5 WHERE id=$1",
                           item.fixture_id, item.home, item.away, item.half_home, item.half_away)
    elif item.status == 'void':
        await conn.execute("UPDATE v2_fixtures SET phase='cancelled',match_state='provider_confirmed_void' WHERE id=$1", item.fixture_id)
    if item.status in {'confirmed', 'void'} or previous and previous['status'] in {'confirmed', 'void'}:
        # A provisional correction invalidates settlement without reopening betting.
        await conn.execute("UPDATE v2_current_quotes SET status='closed' WHERE fixture_id=$1", item.fixture_id)
    return {'version': version, 'duplicate': False, **await project_settlements(conn, fixture, version, facts)}
