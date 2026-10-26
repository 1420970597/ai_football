import json
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import asyncpg
from fastapi import FastAPI
import httpx

from app import database
from app.database import apply_migrations
from app.routes import expert_content, public, results


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
        self.app.state.settings = SimpleNamespace(collector_token='isolated-collector', quote_max_age_seconds=60)
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

    async def signal(self, selection='over', line='2.25', phase='scheduled', period='full_time'):
        signal = uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_expert_signals(id,message_id,expert_id,fixture_id,phase,market_id,market_name,market_kind,period,line,selection,recommendation_price,quote_time,confidence) VALUES($1,$2,$3,'fixture',$4,'total','大小','total',$5,$6,$7,2,now(),0.8)", signal, self.message, self.expert, phase, period, line, selection)
        return signal

    def result(self, **extra):
        return {'fixture_id':'fixture','provider_reference':'official-1','status':'confirmed','home':1,'away':1,
                'source_updated_at':self.now.isoformat(),'evidence':{'source':'isolated-official-fixture'}, **extra}

    async def submit(self, body):
        return await self.client.post('/internal/collector/results', json={'items':[body]}, headers={'Authorization':'Bearer isolated-collector'})

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

    async def test_message_detail_exposes_quote_id_from_evidence_without_requiring_current_quote(self):
        signal = await self.signal()
        quote_id = str(uuid.uuid4())
        await self.pool.execute(
            "INSERT INTO v2_quote_evidence(id,evidence_kind,reference_id,fixture_id,market_id,market_name,selection,price,source_updated_at,evidence) "
            "VALUES($1,'expert_signal',$2,'fixture','total','大小','over',2,now(),$3)",
            uuid.uuid4(), signal, {'quote_id': quote_id},
        )
        with patch.object(expert_content, 'require_history', AsyncMock()):
            response = await self.client.get(f'/api/v1/public/messages/{self.message}')
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(quote_id, response.json()['signals'][0]['quote_id'])

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
        self.assertEqual(422, (await self.submit(self.result(source_updated_at=(self.now+timedelta(days=1)).isoformat()))).status_code)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_fixture_results'))

    async def test_unknown_time_quotes_and_chinese_calendar(self):
        await self.pool.execute("UPDATE v2_fixtures SET phase='finished',kickoff_at='2026-10-25T16:30:00Z'")
        await self.pool.execute("INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,selection,price,source_updated_at,status) VALUES($1,'fixture','total','大小','over',2,NULL,'active')", uuid.uuid4())
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

    async def test_exact_league_multiselect_does_not_match_similar_names(self):
        await self.pool.execute("UPDATE v2_fixtures SET competition_id='league-1'")
        selected = await self.client.get('/api/v1/public/matches', params=[('league_ids','league-1'),('league_ids','league-2')])
        self.assertEqual(1, len(selected.json()['items']))
        other = await self.client.get('/api/v1/public/matches', params={'league_ids':'league'})
        self.assertEqual([], other.json()['items'])
