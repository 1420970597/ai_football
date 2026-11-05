import asyncio
import json
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import asyncpg
from fastapi import FastAPI
import httpx

from app import database
from app.database import apply_migrations
from app.expert_statistics import StatisticsScope,signal_scope,QUALITY_METRIC_SQL
from app.routes import expert_content, public, results
from app import result_ledger
from app.settlement import pending


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires task-owned isolated database')
class ResultLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.admin_url = os.environ['TEST_DATABASE_ADMIN_URL']
        self.dbname = 'test_result_' + uuid.uuid4().hex
        self.admin = await asyncpg.connect(self.admin_url)
        await self.admin.execute(f'CREATE DATABASE "{self.dbname}"')
        async def codecs(conn):
            await conn.set_type_codec('jsonb', schema='pg_catalog', encoder=json.dumps, decoder=json.loads)
        self.pool = await asyncpg.create_pool(self.admin_url.rsplit('/', 1)[0]+'/'+self.dbname, init=codecs, min_size=1, max_size=3)
        async with self.pool.acquire() as conn:
            await apply_migrations(conn)
        database.pool = self.pool
        self.app = FastAPI()
        self.app.state.settings = SimpleNamespace(collector_token='isolated-collector', quote_max_age_seconds=60, session_secret=b'k'*32)
        for router in (results.router, public.router, expert_content.router):
            self.app.include_router(router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test')
        self.expert, self.source, self.message = [uuid.uuid4() for _ in range(3)]
        self.now = datetime.now(timezone.utc)
        await self.pool.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,'chat','source')", self.source)
        await self.pool.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,'author','真实昵称')", self.expert)
        await self.pool.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) VALUES($1,$2,$3,'1','原文',now())", self.message, self.source, self.expert)
        await self.pool.execute("INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase) VALUES('fixture','fixture','football','League','Alpha','Beta',$1,'live')", self.now)

    async def asyncTearDown(self):
        await self.client.aclose()
        database.pool = None
        await self.pool.close()
        await self.admin.execute(f'DROP DATABASE "{self.dbname}" WITH (FORCE)')
        await self.admin.close()

    async def signal(self, selection='over', line='2.25', phase='scheduled', period='full_time', *, evidence=True):
        signal = uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_expert_signals(id,message_id,expert_id,fixture_id,phase,market_id,market_name,market_kind,period,line,selection,recommendation_price,quote_time,confidence) VALUES($1,$2,$3,'fixture',$4,'total','大小','total',$5,$6,$7,2,now(),0.8)", signal, self.message, self.expert, phase, period, line, selection)
        if evidence:
            await self.pool.execute(
                "INSERT INTO v2_quote_evidence(id,evidence_kind,reference_id,fixture_id,market_id,market_name,period,line,selection,price,source_updated_at,evidence,score_baseline) "
                "SELECT $1,'expert_signal',id,fixture_id,market_id,market_name,period,line,selection,recommendation_price,quote_time,"
                "jsonb_build_object('quote_id',$2::text,'score_basis',score_basis),score_baseline FROM v2_expert_signals WHERE id=$3",
                uuid.uuid4(), str(uuid.uuid4()), signal)
        return signal

    def result(self, **extra):
        return {'fixture_id':'fixture','provider_reference':'official-1','status':'confirmed','home':1,'away':1,
                'source_updated_at':self.now.isoformat(),'evidence':{'source':'isolated-official-fixture'}, **extra}

    async def submit(self, body):
        return await self.client.post('/internal/collector/results', json={'items':[body]}, headers={'Authorization':'Bearer isolated-collector'})

    async def assert_settlement_lock_allows_reference_but_blocks_writers(self, mode):
        await self.signal()
        self.assertEqual(200, (await self.submit(self.result())).status_code)
        job, attempt = uuid.uuid4(), uuid.uuid4()
        await self.pool.execute(
            "INSERT INTO v2_analysis_jobs(id,message_id,source_id,expert_id,state,attempt_id,lease_until,idempotency_key) "
            "VALUES($1,$2,$3,$4,'running',$5,now()+interval '90 seconds',$6)",
            job, self.message, self.source, self.expert, attempt, str(job))
        entered, release = asyncio.Event(), asyncio.Event()
        original = result_ledger.project_settlements

        async def paused(*args):
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
            return await original(*args)

        async def settle():
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    if mode == 'rebuild':
                        return await result_ledger.rebuild_settlements(conn, 'fixture')
                    item = results.ResultInput(**self.result(home=3, source_updated_at=(self.now+timedelta(seconds=1)).isoformat()))
                    return await result_ledger.ingest_result(conn, item)

        with patch.object(result_ledger, 'project_settlements', side_effect=paused):
            writer = asyncio.create_task(settle())
            await asyncio.wait_for(entered.wait(), 3)
            binding_error = None
            try:
                async with self.pool.acquire() as conn:
                    await conn.execute("SET lock_timeout='800ms'")
                    try:
                        await conn.execute(
                            "INSERT INTO v2_review_fixture_bindings(job_id,attempt_id,fixture_id,search_key,evidence) "
                            "VALUES($1,$2,'fixture','isolated-reference','{}')", job, attempt)
                    except asyncpg.LockNotAvailableError as exc:
                        binding_error = type(exc).__name__
                    await conn.execute("SET lock_timeout='100ms'")
                    with self.assertRaises(asyncpg.LockNotAvailableError):
                        await conn.fetchrow("SELECT id FROM v2_fixtures WHERE id='fixture' FOR NO KEY UPDATE")
            finally:
                release.set()
                await writer
        self.assertIsNone(binding_error, 'a settlement that never changes fixture keys must allow audit references')
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings WHERE job_id=$1', job))

    async def test_rebuild_fixture_lock_allows_audit_reference_and_serializes_writers(self):
        await self.assert_settlement_lock_allows_reference_but_blocks_writers('rebuild')

    async def test_result_ingestion_lock_allows_audit_reference_and_serializes_writers(self):
        await self.assert_settlement_lock_allows_reference_but_blocks_writers('ingest')

    async def test_large_settlement_preserves_concurrent_fixture_binding_lock_budget(self):
        signals = [await self.signal(line=str(Decimal('2') + Decimal(index) / 4)) for index in range(8)]
        self.assertEqual(200, (await self.submit(self.result(home=5, away=1))).status_code)
        job, attempt = uuid.uuid4(), uuid.uuid4()
        await self.pool.execute(
            "INSERT INTO v2_analysis_jobs(id,message_id,source_id,expert_id,state,attempt_id,lease_until,idempotency_key) "
            "VALUES($1,$2,$3,$4,'running',$5,now()+interval '90 seconds',$6)",
            job, self.message, self.source, self.expert, attempt, str(job))
        latest = await self.pool.fetchrow("SELECT * FROM v2_fixture_results WHERE fixture_id='fixture'")
        facts = result_ledger.result_facts(latest)
        locked = asyncio.Event()

        class RemoteConnection:
            # A conservative WAN delay at the connection boundary reproduces
            # the measured 255ms remote round trip without production access.
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                method = getattr(self.connection, name)
                if name not in {'execute', 'fetch', 'fetchrow', 'fetchval'}:
                    return method

                async def delayed(*args, **kwargs):
                    await asyncio.sleep(.15)
                    return await method(*args, **kwargs)
                return delayed

        async def project():
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    fixture = await conn.fetchrow("SELECT id,sport FROM v2_fixtures WHERE id='fixture' FOR UPDATE")
                    locked.set()
                    return await result_ledger.project_settlements(RemoteConnection(conn), fixture, latest['version'], facts)

        writer = asyncio.create_task(project())
        await asyncio.wait_for(locked.wait(), 3)
        binding_error = None
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("SET lock_timeout='1500ms'")
                await conn.execute(
                    "INSERT INTO v2_review_fixture_bindings(job_id,attempt_id,fixture_id,search_key,evidence) "
                    "VALUES($1,$2,'fixture','isolated-binding','{}')", job, attempt)
        except asyncpg.LockNotAvailableError as exc:
            binding_error = type(exc).__name__
        finally:
            projected = await writer
        self.assertIsNone(binding_error, 'settlement held the fixture beyond the existing binding lock budget')
        self.assertEqual({'settled': 8, 'signals': 8}, projected)
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings WHERE job_id=$1', job))
        self.assertEqual(8, await self.pool.fetchval('SELECT count(*) FROM v2_signal_settlements'))
        self.assertEqual(['win'] * 8, [r['outcome'] for r in await self.pool.fetch('SELECT outcome FROM v2_expert_signals WHERE id=ANY($1::uuid[])', signals)])

    async def test_native_away_home_view_line_rebuild_keeps_raw_snapshot_and_prior_ledger(self):
        signal = await self.signal(selection='away', line='1')
        await self.pool.execute("INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase) VALUES('leyu:360','leyu:360','football','League','Alpha','Beta',$1,'scheduled')", self.now)
        await self.pool.execute("UPDATE v2_expert_signals SET fixture_id='leyu:360',market_id='4',market_kind='handicap' WHERE id=$1", signal)
        await self.pool.execute("UPDATE v2_quote_evidence SET fixture_id='leyu:360',market_id='4' WHERE reference_id=$1", signal)
        with patch.object(result_ledger, 'ALGORITHM_VERSION', 'football-asian-3-zero-baseline'), \
             patch.object(result_ledger, 'line_basis', return_value='selection'):
            response = await self.submit(self.result(fixture_id='leyu:360', home=1, away=2))
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('win', await self.pool.fetchval('SELECT outcome FROM v2_expert_signals WHERE id=$1', signal))
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await result_ledger.rebuild_settlements(conn, 'leyu:360')
        row = await self.pool.fetchrow('SELECT outcome,unit_net,line FROM v2_expert_signals WHERE id=$1', signal)
        self.assertEqual(('push', 0, 1), tuple(row.values()))
        ledgers = await self.pool.fetch('SELECT outcome,calculation FROM v2_signal_settlements WHERE signal_id=$1 ORDER BY algorithm_version', signal)
        self.assertEqual(['win','push'], [r['outcome'] for r in ledgers])
        self.assertEqual('provider_home', ledgers[1]['calculation']['signal']['line_basis'])
        self.assertEqual('1.000', ledgers[1]['calculation']['signal']['line'])
        self.assertEqual(1, await self.pool.fetchval('SELECT line FROM v2_quote_evidence WHERE reference_id=$1', signal))
        public_response = await self.client.get('/api/v1/public/signals')
        self.assertEqual(200, public_response.status_code)
        item = public_response.json()['items'][0]
        self.assertEqual('provider_home', item['line_basis'])
        self.assertEqual(-1, float(item['selection_line']))

    async def test_versioned_correction_preserves_previous_calculation(self):
        signal = await self.signal()
        first = await self.submit(self.result())
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual('half_loss', await self.pool.fetchval('SELECT outcome FROM v2_expert_signals WHERE id=$1', signal))

        duplicate = await self.submit(self.result())
        self.assertTrue(duplicate.json()['items'][0]['duplicate'])
        corrected = await self.submit(self.result(home=3, source_updated_at=(self.now+timedelta(seconds=1)).isoformat()))
        self.assertEqual(200, corrected.status_code, corrected.text)
        self.assertEqual('win', await self.pool.fetchval('SELECT outcome FROM v2_expert_signals WHERE id=$1', signal))
        self.assertEqual(['half_loss','win'], [r['outcome'] for r in await self.pool.fetch('SELECT outcome FROM v2_signal_settlements ORDER BY result_version')])
        self.assertEqual(409, (await self.submit(self.result())).status_code)

    async def test_settled_signal_exposes_current_fixture_phase_separately(self):
        signal = await self.signal(phase='scheduled')
        response = await self.submit(self.result(home=3))
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('win', await self.pool.fetchval('SELECT outcome FROM v2_expert_signals WHERE id=$1', signal))
        self.assertEqual(('finished', 'finished'), tuple(await self.pool.fetchrow(
            'SELECT phase,match_state FROM v2_fixtures WHERE id=$1', 'fixture')))

        listing = await self.client.get('/api/v1/public/signals')
        self.assertEqual(200, listing.status_code, listing.text)
        listed = next(item for item in listing.json()['items'] if item['id'] == str(signal))
        self.assertEqual('finished', listed['fixture_phase'])
        self.assertEqual('finished', listed['match_state'])
        self.assertEqual('scheduled', listed['phase'])
        self.assertEqual('win', listed['outcome'])

        match = await self.client.get('/api/v1/public/matches/fixture')
        self.assertEqual(200, match.status_code, match.text)
        matched = next(item for item in match.json()['expert_signals'] if item['id'] == str(signal))
        self.assertEqual(('finished', 'scheduled', 'win'),
                         (matched['fixture_phase'], matched['phase'], matched['outcome']))

        actor = {'id': str(self.expert), 'role': 'admin'}
        with patch.object(expert_content, 'require_history', AsyncMock(return_value=actor)):
            message = await self.client.get(f'/api/v1/public/messages/{self.message}/signals')
        self.assertEqual(200, message.status_code, message.text)
        messaged = next(item for item in message.json()['items'] if item['id'] == str(signal))
        self.assertEqual(('finished', 'scheduled', 'win'),
                         (messaged['fixture_phase'], messaged['phase'], messaged['outcome']))

    async def test_message_detail_exposes_quote_id_from_evidence_without_requiring_current_quote(self):
        signal = await self.signal(evidence=False)
        quote_id = str(uuid.uuid4())
        await self.pool.execute(
            "INSERT INTO v2_quote_evidence(id,evidence_kind,reference_id,fixture_id,market_id,market_name,line,selection,price,source_updated_at,evidence) "
            "VALUES($1,'expert_signal',$2,'fixture','total','大小',2.25,'over',2,now(),$3)",
            uuid.uuid4(), signal, {'quote_id': quote_id},
        )
        with patch.object(expert_content, 'require_history', AsyncMock()):
            response = await self.client.get(f'/api/v1/public/messages/{self.message}')
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(quote_id, response.json()['signals'][0]['quote_id'])

    async def test_public_fixture_pagination_rejects_changed_filters_and_tampering(self):
        await self.pool.execute("INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase) "
                                "VALUES('second','second','football','League','Gamma','Delta',$1,'scheduled')", self.now)
        first = await self.client.get('/api/v1/public/matches', params={'page_size':1})
        cursor = first.json()['next_cursor']
        self.assertTrue(cursor)
        second = await self.client.get('/api/v1/public/matches', params={'page_size':1, 'cursor':cursor})
        self.assertEqual(200, second.status_code, second.text)
        self.assertNotEqual(first.json()['items'][0]['id'], second.json()['items'][0]['id'])
        changed = await self.client.get('/api/v1/public/matches', params={'q':'Alpha', 'cursor':cursor})
        self.assertEqual(400, changed.status_code)
        tampered = await self.client.get('/api/v1/public/matches', params={'cursor':'AAAA'+cursor[4:]})
        self.assertEqual(400, tampered.status_code)

    async def test_provisional_retraction_clears_profit_but_keeps_history(self):
        await self.signal()
        await self.submit(self.result(home=3))
        pending = await self.submit(self.result(status='provisional', source_updated_at=(self.now+timedelta(seconds=1)).isoformat()))
        self.assertEqual(200, pending.status_code, pending.text)
        row = await self.pool.fetchrow('SELECT * FROM v2_expert_signals')
        self.assertEqual('pending', row['outcome'])
        self.assertIsNone(row['unit_net'])
        self.assertIsNone(row['settled_at'])
        self.assertEqual(2, await self.pool.fetchval('SELECT count(*) FROM v2_signal_settlements'))

    async def test_live_unknown_rule_does_not_invent_profit(self):
        await self.signal(phase='live')
        response = await self.submit(self.result(home=3))
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('live_market_score_rule_unknown', await self.pool.fetchval('SELECT settlement_reason FROM v2_expert_signals'))

    async def test_zero_baseline_rebuild_preserves_old_pending_and_projects_loss_once(self):
        signal = await self.signal(phase='live', period='first_half', line='.75')
        baseline = dict(home=0, away=0, half_home=0, half_away=0)
        await self.pool.execute('UPDATE v2_expert_signals SET score_baseline=$2 WHERE id=$1', signal, baseline)
        await self.pool.execute('UPDATE v2_quote_evidence SET score_baseline=$2 WHERE reference_id=$1', signal, baseline)
        # Reproduce the previous algorithm's receipt, before enabling the new one.
        with patch.object(result_ledger, 'ALGORITHM_VERSION', 'football-asian-2-evidence'), \
             patch.object(result_ledger, 'settle', return_value=pending('live_market_score_rule_unknown')):
            response = await self.submit(self.result(home=0, away=0, half_home=0, half_away=0))
        self.assertEqual(200, response.status_code, response.text)
        old = dict(await self.pool.fetchrow('SELECT * FROM v2_signal_settlements WHERE signal_id=$1', signal))
        self.assertEqual('pending', old['outcome'])
        for _ in range(2):
            response = await self.client.post('/internal/collector/results/rebuild',
                json={'fixture_ids': ['fixture']}, headers={'Authorization': 'Bearer isolated-collector'})
            self.assertEqual(200, response.status_code, response.text)
        rows = await self.pool.fetch('SELECT * FROM v2_signal_settlements WHERE signal_id=$1 ORDER BY algorithm_version', signal)
        self.assertEqual(2, len(rows))
        self.assertEqual(old, dict(rows[0]))
        self.assertEqual(('loss', -1, 1), tuple(rows[1][key] for key in ('outcome', 'unit_net', 'effective_stake')))
        self.assertTrue(rows[1]['reason'].endswith(':zero_baseline_score_equivalent'))
        projected = await self.pool.fetchrow('SELECT outcome,unit_net,settled_at,score_basis FROM v2_expert_signals WHERE id=$1', signal)
        self.assertEqual(('loss', -1, rows[1]['created_at'], 'unknown'), tuple(projected))
        summary = (await self.client.get('/api/v1/public/experts')).json()['items'][0]
        self.assertEqual((1, -1, 0), tuple(summary[key] for key in ('settled_signals', 'unit_net', 'invalid_settlement_evidence')))
        match = await self.client.get('/api/v1/public/matches/fixture')
        self.assertEqual(200, match.status_code, match.text)
        projected_match = next(s for s in match.json()['expert_signals'] if s['id'] == str(signal))
        self.assertEqual(('first_half', 'total', baseline, 'finished', 'live', 'loss', -1),
                         tuple(projected_match[key] for key in ('period', 'market_kind', 'score_baseline', 'fixture_phase', 'phase', 'outcome', 'unit_net')))

    async def test_nonzero_equivalence_rebuild_keeps_prior_ledger_quote_and_unknown_rule(self):
        signal = await self.signal(phase='live', line='1.75')
        baseline = dict(home=0, away=1, half_home=0, half_away=0)
        await self.pool.execute('UPDATE v2_expert_signals SET score_baseline=$2 WHERE id=$1', signal, baseline)
        await self.pool.execute('UPDATE v2_quote_evidence SET score_baseline=$2 WHERE reference_id=$1', signal, baseline)
        quote = [dict(r) for r in await self.pool.fetch('SELECT * FROM v2_quote_evidence WHERE reference_id=$1', signal)]
        with patch.object(result_ledger, 'ALGORITHM_VERSION', 'football-asian-4-native-line-basis'), \
             patch.object(result_ledger, 'settle', return_value=pending('live_market_score_rule_unknown')):
            response = await self.submit(self.result(home=0, away=1, half_home=0, half_away=1))
        self.assertEqual(200, response.status_code, response.text)
        old = dict(await self.pool.fetchrow('SELECT * FROM v2_signal_settlements WHERE signal_id=$1', signal))
        for _ in range(2):
            response = await self.client.post('/internal/collector/results/rebuild',
                json={'fixture_ids':['fixture']}, headers={'Authorization':'Bearer isolated-collector'})
            self.assertEqual(200, response.status_code, response.text)
        rows = await self.pool.fetch('SELECT * FROM v2_signal_settlements WHERE signal_id=$1 ORDER BY algorithm_version', signal)
        self.assertEqual(2, len(rows))
        self.assertEqual(old, dict(rows[0]))
        self.assertEqual(('loss', -1, 1), tuple(rows[1][k] for k in ('outcome','unit_net','effective_stake')))
        self.assertTrue(rows[1]['reason'].endswith(':score_basis_outcome_equivalent'))
        self.assertEqual(quote, [dict(r) for r in await self.pool.fetch('SELECT * FROM v2_quote_evidence WHERE reference_id=$1', signal)])
        projected = await self.pool.fetchrow('SELECT outcome,unit_net,score_basis,score_baseline FROM v2_expert_signals WHERE id=$1', signal)
        self.assertEqual(('loss', -1, 'unknown', baseline), tuple(projected))
        # A later confirmed score change can make the two interpretations
        # disagree. Retract the projection, retaining both prior receipts.
        response = await self.submit(self.result(home=1, away=1, half_home=0, half_away=1,
            source_updated_at=(self.now+timedelta(seconds=1)).isoformat()))
        self.assertEqual(200, response.status_code, response.text)
        projected = await self.pool.fetchrow('SELECT outcome,unit_net,settled_at FROM v2_expert_signals WHERE id=$1', signal)
        self.assertEqual(('pending', None, None), tuple(projected))
        self.assertEqual(3, await self.pool.fetchval('SELECT count(*) FROM v2_signal_settlements WHERE signal_id=$1', signal))
        self.assertEqual(old, dict(await self.pool.fetchrow('SELECT * FROM v2_signal_settlements WHERE signal_id=$1 AND algorithm_version=$2', signal, 'football-asian-4-native-line-basis')))

    async def test_missing_or_mismatched_immutable_price_never_creates_profit(self):
        missing = await self.signal(evidence=False)
        mismatch = await self.signal(line='3.5')
        await self.pool.execute('UPDATE v2_quote_evidence SET price=3 WHERE reference_id=$1', mismatch)
        response = await self.submit(self.result(home=4))
        self.assertEqual(200, response.status_code, response.text)
        rows = await self.pool.fetch('SELECT outcome,unit_net,settlement_reason FROM v2_expert_signals')
        self.assertTrue(all(row['outcome']=='pending' and row['unit_net'] is None for row in rows))
        self.assertTrue(all(row['settlement_reason']=='recommendation_quote_evidence_missing_or_mismatched' for row in rows))
        summary = (await self.client.get('/api/v1/public/experts')).json()['items'][0]
        self.assertEqual((2,0,2,2), tuple(summary[k] for k in ('total_signals','eligible_signals','excluded_signals','missing_quote_evidence')))
        self.assertIsNone(summary['accuracy'])
        self.assertEqual(0, summary['settled_signals'])

    async def test_projection_rebuild_is_idempotent_and_preserves_settlement_time(self):
        signal = await self.signal()
        self.assertEqual(200, (await self.submit(self.result(home=3))).status_code)
        original = await self.pool.fetchrow('SELECT outcome,unit_net,settled_at FROM v2_expert_signals WHERE id=$1', signal)
        await self.pool.execute("UPDATE v2_expert_signals SET outcome='loss',unit_net=-99,effective_stake=1 WHERE id=$1", signal)
        excluded = (await self.client.get('/api/v1/public/experts')).json()['items'][0]
        self.assertEqual(1, excluded['invalid_settlement_evidence'])
        self.assertEqual(0, excluded['unit_net'])
        for _ in range(2):
            response = await self.client.post('/internal/collector/results/rebuild',json={'fixture_ids':['fixture']},headers={'Authorization':'Bearer isolated-collector'})
            self.assertEqual(200, response.status_code, response.text)
        restored = await self.pool.fetchrow('SELECT outcome,unit_net,settled_at FROM v2_expert_signals WHERE id=$1', signal)
        self.assertEqual(dict(original), dict(restored))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_signal_settlements'))

    async def test_rebuild_rejects_immutable_calculation_conflict_and_missing_result(self):
        headers = {'Authorization':'Bearer isolated-collector'}
        url = '/internal/collector/results/rebuild'
        self.assertEqual(401, (await self.client.post(url,json={'fixture_ids':['fixture']})).status_code)
        self.assertEqual(409, (await self.client.post(url,json={'fixture_ids':['fixture']},headers=headers)).status_code)
        self.assertEqual(422, (await self.client.post(url,json={'fixture_ids':['fixture','fixture']},headers=headers)).status_code)
        await self.signal()
        await self.submit(self.result(home=3))
        await self.pool.execute("UPDATE v2_quote_evidence SET price=2.5")
        response = await self.client.post(url,json={'fixture_ids':['fixture']},headers=headers)
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_signal_settlements'))

    async def test_rebuild_legacy_algorithm_preserves_first_calculation_timestamp(self):
        await self.signal()
        await self.submit(self.result(home=3))
        historical = self.now-timedelta(days=3)
        await self.pool.execute("UPDATE v2_signal_settlements SET algorithm_version='football-asian-1',created_at=$1", historical)
        await self.pool.execute('UPDATE v2_expert_signals SET settled_at=$1', historical)
        response = await self.client.post('/internal/collector/results/rebuild',json={'fixture_ids':['fixture']},headers={'Authorization':'Bearer isolated-collector'})
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual(historical,await self.pool.fetchval('SELECT settled_at FROM v2_expert_signals'))
        self.assertEqual(2,await self.pool.fetchval('SELECT count(*) FROM v2_signal_settlements'))

    async def test_settlement_time_window_filters_and_nickname_identity(self):
        signal = await self.signal()
        await self.pool.execute("UPDATE v2_expert_signals SET created_at=now()-interval '70 days' WHERE id=$1", signal)
        await self.submit(self.result(home=3))
        await self.pool.execute("UPDATE v2_experts SET display_nickname='最新昵称' WHERE id=$1", self.expert)
        params = {'source_id':str(self.source),'phase':'scheduled','market_kind':'total','days':1}
        listing = (await self.client.get('/api/v1/public/experts',params=params)).json()
        detail = (await self.client.get(f'/api/v1/public/experts/{self.expert}',params=params)).json()
        trend = (await self.client.get(f'/api/v1/public/experts/{self.expert}/trend',params=params)).json()
        self.assertEqual('最新昵称', listing['items'][0]['display_nickname'])
        self.assertEqual(1, listing['items'][0]['settled_signals'])
        self.assertEqual(listing['items'][0], detail['expert'])
        self.assertEqual([{'id':str(self.source),'display_name':'source'}],listing['source_options'])
        self.assertEqual(detail['expert'], trend['summary'])
        self.assertEqual(1, trend['items'][0]['cumulative_settled'])
        for params in ({'phase':'live'}, {'market_kind':'winner'}, {'source_id':str(uuid.uuid4())}):
            value = (await self.client.get('/api/v1/public/experts',params=params)).json()['items'][0]
            self.assertEqual(0,value['total_signals'])
        for params in ({'phase':'finished'},{'market_kind':'unknown'},{'source_id':'invalid'},
                       {'from_utc':'2026-10-26T00:00:00'},
                       {'from_utc':'2026-10-27T00:00:00Z','to_utc':'2026-10-26T00:00:00Z'}):
            self.assertEqual(422,(await self.client.get('/api/v1/public/experts',params=params)).status_code)

    async def test_same_nickname_cross_group_stays_distinct_and_source_options_hide_disabled(self):
        await self.signal()
        other_source, other_expert, other_message = [uuid.uuid4() for _ in range(3)]
        await self.pool.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,'other-chat','其他来源')",other_source)
        await self.pool.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,'other-author','真实昵称')",other_expert)
        await self.pool.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) VALUES($1,$2,$3,'1','原文',now())",other_message,other_source,other_expert)
        original = self.message,self.expert
        self.message,self.expert = other_message,other_expert
        try:
            await self.signal(selection='under',line='3')
        finally:
            self.message,self.expert = original
        await self.submit(self.result(home=3))
        listing=(await self.client.get('/api/v1/public/experts')).json()
        self.assertEqual(2,len(listing['items']))
        self.assertEqual(2,len({item['id'] for item in listing['items']}))
        selected=(await self.client.get('/api/v1/public/experts',params={'source_id':str(other_source)})).json()
        self.assertEqual(1,sum(item['total_signals'] for item in selected['items']))
        self.assertEqual(2,len(selected['source_options']))
        await self.pool.execute('UPDATE v2_expert_sources SET enabled=false WHERE id=$1',other_source)
        refreshed=(await self.client.get('/api/v1/public/experts')).json()
        self.assertEqual([{'id':str(self.source),'display_name':'source'}],refreshed['source_options'])

    async def test_materialized_quality_matches_original_through_stale_quotes_and_result_revisions(self):
        valid=await self.signal()
        await self.signal(selection='under',line='3',evidence=False)
        # Recommendation evidence may be old: freshness applies at acceptance,
        # not when computing historical returns after an official result.
        await self.pool.execute("UPDATE v2_expert_signals SET quote_time=quote_time-interval '90 days' WHERE id=$1",valid)
        await self.pool.execute("UPDATE v2_quote_evidence SET source_updated_at=source_updated_at-interval '90 days' WHERE reference_id=$1",valid)
        async def compare():
            scope=StatisticsScope(self.now-timedelta(days=1),self.now+timedelta(days=1))
            prefix,args=signal_scope(scope,self.expert)
            sql=prefix+'SELECT '+QUALITY_METRIC_SQL+' FROM scoped s'
            current=dict(await self.pool.fetchrow(sql,*args))
            original=dict(await self.pool.fetchrow(sql.replace('WITH checked AS MATERIALIZED (','WITH checked AS (',1),*args))
            self.assertEqual(original,current)
            return current
        await self.submit(self.result(home=3))
        current=await compare()
        self.assertEqual((1,1,1),tuple(current[key] for key in ('eligible_signals','excluded_signals','win')))
        await self.submit(self.result(status='provisional',source_updated_at=(self.now+timedelta(seconds=1)).isoformat()))
        self.assertEqual(1,(await compare())['pending'])
        await self.submit(self.result(home=0,source_updated_at=(self.now+timedelta(seconds=2)).isoformat()))
        self.assertEqual(1,(await compare())['loss'])
        await self.pool.execute("UPDATE v2_expert_signals SET result_version=1,outcome='win',unit_net=1 WHERE id=$1",valid)
        self.assertEqual(1,(await compare())['invalid_settlement_evidence'])

    async def test_result_timezone_is_canonical_and_void_closes_fixture(self):
        await self.signal()
        first = await self.submit(self.result())
        same_in_another_zone = self.now.astimezone(timezone(timedelta(hours=8))).isoformat()
        replay = await self.submit(self.result(source_updated_at=same_in_another_zone))
        self.assertEqual(200, replay.status_code, replay.text)
        self.assertTrue(replay.json()['items'][0]['duplicate'])
        cancelled = await self.submit(self.result(status='void',source_updated_at=(self.now+timedelta(seconds=1)).isoformat(),home=None,away=None))
        self.assertEqual(200,cancelled.status_code,cancelled.text)
        self.assertEqual('cancelled',await self.pool.fetchval('SELECT phase FROM v2_fixtures'))
        self.assertEqual('void',await self.pool.fetchval('SELECT outcome FROM v2_expert_signals'))

    async def test_statistics_split_effective_stake_and_average_return(self):
        await self.signal()
        await self.signal(selection='under', line='2')
        response = await self.submit(self.result())
        self.assertEqual(200, response.status_code, response.text)
        summary = (await self.client.get('/api/v1/public/experts')).json()['items'][0]
        self.assertEqual(1, summary['half_loss'])
        self.assertEqual(1, summary['push'])
        self.assertEqual(-0.5, summary['unit_net'])
        self.assertEqual(-0.25, summary['average_unit_net'])
        self.assertEqual(-1, summary['unit_roi'])
        self.assertEqual(0, summary['accuracy'])
        trend = (await self.client.get(f'/api/v1/public/experts/{self.expert}/trend')).json()['items']
        self.assertEqual(-0.25, float(trend[0]['average_unit_net']))

    async def test_result_contract_auth_and_invalid_scores(self):
        payload = {'items':[self.result()]}
        self.assertEqual(401, (await self.client.post('/internal/collector/results', json=payload)).status_code)
        self.assertEqual(422, (await self.submit(self.result(half_home=9, half_away=0))).status_code)
        self.assertEqual(422, (await self.submit(self.result(home=None))).status_code)
        self.assertEqual(422, (await self.submit(self.result(home=True))).status_code)
        self.assertEqual(422, (await self.submit(self.result(home='1'))).status_code)
        self.assertEqual(422, (await self.submit(self.result(provider_reference=' '))).status_code)
        self.assertEqual(422, (await self.submit(self.result(evidence={'source':' '}))).status_code)
        self.assertEqual(422, (await self.submit(self.result(source_updated_at=(self.now+timedelta(days=1)).isoformat()))).status_code)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_fixture_results'))

    async def test_unknown_time_quotes_and_chinese_calendar(self):
        await self.pool.execute("UPDATE v2_fixtures SET phase='finished',kickoff_at='2026-10-25T16:30:00Z'")
        await self.pool.execute("INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,line,selection,price,source_updated_at,status) VALUES($1,'fixture','total','大小',2.5,'over',2,NULL,'active')", uuid.uuid4())
        response = await self.client.get('/api/v1/public/matches', params={'date':'2026-10-26','phase':'finished','tz':'Asia/Shanghai'})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, len(response.json()['items']))
        self.assertFalse(response.json()['items'][0]['quote_fresh'])
        detail = await self.client.get('/api/v1/public/matches/fixture')
        self.assertEqual(200, detail.status_code, detail.text)
        self.assertFalse(detail.json()['markets'][0]['fresh'])
        self.assertEqual([], (await self.client.get('/api/v1/public/matches', params={'date':'2026-10-25','phase':'finished'})).json()['items'])

    async def test_guest_cannot_read_original_or_media(self):
        self.assertEqual(403, (await self.client.get(f'/api/v1/public/messages/{self.message}')).status_code)
        self.assertEqual(403, (await self.client.get(f'/api/v1/public/messages/{self.message}/media/0')).status_code)
        self.assertEqual(403, (await self.client.get(f'/api/v1/public/experts/{self.expert}/messages')).status_code)

    async def test_public_summary_matches_window_ranking_at_maximum_and_paginated_sizes(self):
        fixture_ids = [f'summary-{number:03d}' for number in range(205)]
        await self.pool.executemany(
            "INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase) "
            "VALUES($1,$1,'football','summary-test','Alpha','Beta','2026-10-26T12:00:00Z',$2)",
            [(identifier, ('scheduled', 'live', 'finished', 'cancelled')[number % 4])
             for number, identifier in enumerate(fixture_ids)])
        quotes = []
        for number, fixture_id in enumerate(fixture_ids):
            if number % 7 == 0:
                continue
            for option in range(31):
                quotes.append((uuid.uuid4(), fixture_id, f'market-{option // 3:02d}',
                               Decimal(option % 3) / 4, 'over' if option % 2 else 'under',
                               Decimal('1.8') + Decimal(option) / 100,
                               None if option % 5 == 0 else self.now - timedelta(seconds=option % 4),
                               'closed' if option % 3 == 0 else 'active', option % 6 == 0))
        await self.pool.executemany(
            "INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,period,market_kind,"
            "line,selection,price,source_updated_at,status,is_main) "
            "VALUES($1,$2,$3,'大小','full_time','total',$4,$5,$6,$7,$8,$9)", quotes)
        # The previous SQL is an independent reference for membership, field
        # values and display order, including main lines and unknown times.
        reference = await self.pool.fetch(
            "SELECT quote_id,fixture_id,market_id,market_name,period,market_kind,line,selection,"
            "price,status,is_main,source_updated_at,received_at FROM ("
            "SELECT id AS quote_id,fixture_id,market_id,market_name,period,market_kind,line,selection,"
            "price,status,is_main,source_updated_at,received_at,row_number() OVER ("
            "PARTITION BY fixture_id ORDER BY is_main DESC,source_updated_at DESC NULLS LAST,"
            "market_id,line,selection) AS position FROM v2_current_quotes "
            "WHERE fixture_id=ANY($1::text[])) ranked WHERE position<=24 "
            "ORDER BY fixture_id,market_id,line,selection", fixture_ids)
        expected = {}
        for row in reference:
            expected.setdefault(row['fixture_id'], []).append(public.jsonable_encoder(
                public.line_projection(row) | {'fresh': public.fresh_quote(row, 60)}))
        for phase, page_size in [('all', 50), ('all', 200), ('live', 200),
                                 ('scheduled', 200), ('finished', 200), ('cancelled', 200)]:
            with self.subTest(phase=phase, page_size=page_size):
                params = {'phase': phase, 'page_size': page_size, 'date': '2026-10-26',
                          'tz': 'UTC', 'league': 'summary-test'}
                seen = []
                while True:
                    response = await self.client.get('/api/v1/public/matches', params=params)
                    self.assertEqual(200, response.status_code, response.text)
                    body = response.json()
                    for item in body['items']:
                        markets = expected.get(item['id'], [])
                        self.assertEqual(markets, item['markets'])
                        self.assertLessEqual(len(markets), 24)
                        self.assertTrue(item['markets_are_summary'])
                        self.assertEqual(any(row['fresh'] for row in markets) and
                                         item['phase'] in {'live', 'scheduled'}, item['quote_fresh'])
                        if phase != 'all':
                            self.assertEqual(phase, item['phase'])
                        seen.append(item['id'])
                    if not body['next_cursor']:
                        break
                    params['cursor'] = body['next_cursor']
                selected = [identifier for number, identifier in enumerate(fixture_ids)
                            if phase == 'all' or
                            ('scheduled', 'live', 'finished', 'cancelled')[number % 4] == phase]
                self.assertEqual(selected, seen)
        empty = await self.client.get('/api/v1/public/matches', params={'q': 'no-matching-fixture'})
        self.assertEqual(200, empty.status_code)
        self.assertEqual([], empty.json()['items'])

    async def test_exact_league_multiselect_does_not_match_similar_names(self):
        await self.pool.execute("UPDATE v2_fixtures SET competition_id='league-1'")
        selected = await self.client.get('/api/v1/public/matches', params=[('league_ids','league-1'),('league_ids','league-2')])
        self.assertEqual(1, len(selected.json()['items']))
        other = await self.client.get('/api/v1/public/matches', params={'league_ids':'league'})
        self.assertEqual([], other.json()['items'])

    async def test_public_legacy_aliases_ignore_case_and_surrounding_space(self):
        await self.pool.execute("UPDATE v2_fixtures SET home_aliases=ARRAY['  OldALIAS  ']")
        response = await self.client.get('/api/v1/public/matches', params={'q':'oldalias'})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(['fixture'], [item['id'] for item in response.json()['items']])

    async def test_match_market_pages_reach_1201_without_duplicates_and_reject_cross_scope(self):
        await self.pool.execute("INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,line,selection,price,source_updated_at,status) "
                                "SELECT md5('page-market-'||n)::uuid,'fixture','total-'||n,'大小',2.5,'over',2,now(),'active' FROM generate_series(1,1201) n")
        first = await self.client.get('/api/v1/public/matches/fixture')
        self.assertEqual(200, first.status_code, first.text)
        body = first.json()
        self.assertEqual(500, len(body['markets']))
        cursor = body['next_market_cursor']
        self.assertTrue(cursor)
        seen = [item['quote_id'] for item in body['markets']]
        expected = sorted(seen)
        self.assertEqual(expected, seen)
        await self.pool.execute("INSERT INTO v2_fixtures(id,source_id,sport,competition_name,home_team,away_team,kickoff_at,phase) "
                                "VALUES('other','other','football','League','Gamma','Delta',now(),'scheduled')")
        self.assertEqual(400, (await self.client.get('/api/v1/public/matches/other/markets', params={'cursor':cursor})).status_code)
        self.assertEqual(400, (await self.client.get('/api/v1/public/matches/fixture/markets', params={'cursor':'AAAA'+cursor[4:]})).status_code)
        while cursor:
            response = await self.client.get('/api/v1/public/matches/fixture/markets', params={'cursor':cursor})
            self.assertEqual(200, response.status_code, response.text)
            page = response.json()
            self.assertLessEqual(len(page['markets']), 500)
            seen.extend(item['quote_id'] for item in page['markets'])
            cursor = page['next_market_cursor']
        self.assertEqual(1201, len(seen))
        self.assertEqual(sorted(set(seen)), seen)
        self.assertEqual(422, (await self.client.get('/api/v1/public/matches/fixture/markets', params={'page_size':501})).status_code)
        self.assertEqual(404, (await self.client.get('/api/v1/public/matches/missing/markets')).status_code)
        await self.pool.execute("UPDATE v2_fixtures SET phase='finished' WHERE id='fixture'")
        terminal = await self.client.get('/api/v1/public/matches/fixture/markets', params={'page_size':2})
        self.assertFalse(any(item['fresh'] for item in terminal.json()['markets']))
