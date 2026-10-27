"""可追溯赛果版本与专家收益；不修改或冒充真实订单结算。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from .expert_statistics import QUOTE_VALID_SQL
from .settlement import pending, settle

ALGORITHM_VERSION = 'football-asian-2-evidence'


def result_facts(row):
    return {'status': row['status'], 'home': row['home_score'], 'away': row['away_score'],
            'half_home': row['half_home_score'], 'half_away': row['half_away_score'],
            'provider_reference': row['provider_reference'], 'source_updated_at': row['source_updated_at'].isoformat(),
            'evidence': row['evidence']}


async def project_settlements(conn, fixture, version, facts):
    rows = await conn.fetch('SELECT s.*,' + QUOTE_VALID_SQL +
                            ' AS quote_valid FROM v2_expert_signals s WHERE fixture_id=$1 ORDER BY id FOR UPDATE', fixture['id'])
    settled = 0
    for row in rows:
        inputs = {'sport': fixture['sport'], 'kind': row['market_kind'], 'period': row['period'],
                  'phase': row['phase'], 'price': str(row['recommendation_price']), 'line': str(row['line']) if row['line'] is not None else None,
                  'selection': row['selection'], 'score_basis': row['score_basis'], 'score_baseline': row['score_baseline']}
        value = settle(inputs, facts) if row['quote_valid'] else pending('recommendation_quote_evidence_missing_or_mismatched')
        calculation = {'signal': inputs, 'result': facts}
        await conn.execute(
            'INSERT INTO v2_signal_settlements(signal_id,fixture_id,result_version,algorithm_version,outcome,unit_net,effective_stake,reason,calculation,created_at) '
            'VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,COALESCE((SELECT min(old.created_at) FROM v2_signal_settlements old '
            'WHERE old.signal_id=$1 AND old.result_version=$3 AND old.outcome=$5 '
            'AND old.unit_net IS NOT DISTINCT FROM $6 AND old.effective_stake=$7 AND old.reason=$8),now())) '
            'ON CONFLICT(signal_id,result_version,algorithm_version) DO NOTHING',
            row['id'], fixture['id'], version, ALGORITHM_VERSION, value.outcome, value.unit_net,
            value.effective_stake, value.reason, calculation)
        recorded = await conn.fetchrow(
            'SELECT * FROM v2_signal_settlements WHERE signal_id=$1 AND result_version=$2 AND algorithm_version=$3',
            row['id'], version, ALGORITHM_VERSION)
        expected = (value.outcome, value.unit_net, value.effective_stake, value.reason, calculation)
        actual = tuple(recorded[key] for key in ('outcome','unit_net','effective_stake','reason','calculation'))
        if actual != expected:
            raise HTTPException(409, '不可变结算证据与重算不一致，必须保留原记录并核对算法版本')
        # Replaying the same result must not move its return to a new day.
        await conn.execute(
            "UPDATE v2_expert_signals SET outcome=$2,unit_net=$3,effective_stake=$4,settlement_reason=$5,result_version=$6,"
            "settled_at=CASE WHEN $2='pending' THEN NULL ELSE $7::timestamptz END WHERE id=$1",
            row['id'], value.outcome, value.unit_net, value.effective_stake, value.reason, version,
            recorded['created_at'])
        settled += value.outcome != 'pending'
    return {'settled': settled, 'signals': len(rows)}


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
    if item.source_updated_at > datetime.now(timezone.utc) + timedelta(seconds=5):
        raise HTTPException(422, '赛果源时间晚于当前时间')
    fixture = await conn.fetchrow('SELECT id,sport FROM v2_fixtures WHERE id=$1 FOR UPDATE', item.fixture_id)
    if not fixture:
        raise HTTPException(404, '赛果对应比赛不存在')
    previous = await conn.fetchrow('SELECT * FROM v2_fixture_results WHERE fixture_id=$1 ORDER BY version DESC LIMIT 1', item.fixture_id)
    facts = {'status': item.status, 'home': item.home, 'away': item.away, 'half_home': item.half_home,
             'half_away': item.half_away, 'provider_reference': item.provider_reference,
             'source_updated_at': item.source_updated_at.astimezone(timezone.utc).isoformat(), 'evidence': item.evidence}
    digest = hashlib.sha256(json.dumps(facts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if previous and previous['content_hash'] == digest:
        return {'version': previous['version'], 'duplicate': True, 'settled': 0}
    if previous and item.source_updated_at <= previous['source_updated_at']:
        raise HTTPException(409, '旧赛果或同时间冲突赛果不能覆盖当前版本')
    version = previous['version'] + 1 if previous else 1
    await conn.execute(
        'INSERT INTO v2_fixture_results(fixture_id,version,provider_reference,content_hash,status,home_score,away_score,'
        'half_home_score,half_away_score,source_updated_at,evidence) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)',
        item.fixture_id, version, item.provider_reference, digest, item.status, item.home, item.away,
        item.half_home, item.half_away, item.source_updated_at, item.evidence)
    if item.status == 'confirmed':
        await conn.execute("UPDATE v2_fixtures SET phase='finished',match_state='finished',home_score=$2,away_score=$3,half_home_score=$4,half_away_score=$5 WHERE id=$1",
                           item.fixture_id, item.home, item.away, item.half_home, item.half_away)
    elif item.status == 'void':
        await conn.execute("UPDATE v2_fixtures SET phase='cancelled',match_state='provider_confirmed_void' WHERE id=$1", item.fixture_id)
    if item.status in {'confirmed', 'void'} or previous and previous['status'] in {'confirmed', 'void'}:
        # A provisional correction invalidates settlement without reopening betting.
        await conn.execute("UPDATE v2_current_quotes SET status='closed' WHERE fixture_id=$1", item.fixture_id)
    return {'version': version, 'duplicate': False, **await project_settlements(conn, fixture, version, facts)}
