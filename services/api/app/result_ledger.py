"""可追溯赛果版本与专家收益；不修改或冒充真实订单结算。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from .settlement import settle

ALGORITHM_VERSION = 'football-asian-1'


async def ingest_result(conn, item) -> dict:
    if item.source_updated_at > datetime.now(timezone.utc) + timedelta(seconds=5):
        raise HTTPException(422, '赛果源时间晚于当前时间')
    fixture = await conn.fetchrow('SELECT id,sport FROM v2_fixtures WHERE id=$1 FOR UPDATE', item.fixture_id)
    if not fixture:
        raise HTTPException(404, '赛果对应比赛不存在')
    previous = await conn.fetchrow('SELECT * FROM v2_fixture_results WHERE fixture_id=$1 ORDER BY version DESC LIMIT 1', item.fixture_id)
    facts = {'status': item.status, 'home': item.home, 'away': item.away, 'half_home': item.half_home,
             'half_away': item.half_away, 'provider_reference': item.provider_reference,
             'source_updated_at': item.source_updated_at.isoformat(), 'evidence': item.evidence}
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
        item.half_home, item.half_away, item.source_updated_at, item.evidence,
    )
    if item.status == 'confirmed':
        await conn.execute("UPDATE v2_fixtures SET phase='finished',home_score=$2,away_score=$3,half_home_score=$4,half_away_score=$5 WHERE id=$1",
                           item.fixture_id, item.home, item.away, item.half_home, item.half_away)
    if item.status in {'confirmed', 'void'}:
        await conn.execute("UPDATE v2_current_quotes SET status='closed' WHERE fixture_id=$1", item.fixture_id)
    rows = await conn.fetch('SELECT * FROM v2_expert_signals WHERE fixture_id=$1 ORDER BY id FOR UPDATE', item.fixture_id)
    settled = 0
    for row in rows:
        inputs = {'sport': fixture['sport'], 'kind': row['market_kind'], 'period': row['period'],
                  'phase': row['phase'], 'price': str(row['recommendation_price']), 'line': str(row['line']),
                  'selection': row['selection'], 'score_basis': row['score_basis'], 'score_baseline': row['score_baseline']}
        value = settle(inputs, facts)
        await conn.execute(
            'INSERT INTO v2_signal_settlements(signal_id,fixture_id,result_version,algorithm_version,outcome,unit_net,effective_stake,reason,calculation) '
            'VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)', row['id'], item.fixture_id, version, ALGORITHM_VERSION,
            value.outcome, value.unit_net, value.effective_stake, value.reason, {'signal': inputs, 'result': facts},
        )
        await conn.execute(
            "UPDATE v2_expert_signals SET outcome=$2,unit_net=$3,effective_stake=$4,settlement_reason=$5,result_version=$6,"
            "settled_at=CASE WHEN $2='pending' THEN NULL ELSE now() END WHERE id=$1",
            row['id'], value.outcome, value.unit_net, value.effective_stake, value.reason, version,
        )
        settled += value.outcome != 'pending'
    return {'version': version, 'duplicate': False, 'settled': settled, 'signals': len(rows)}
