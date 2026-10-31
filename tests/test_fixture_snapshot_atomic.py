"""Bounded full snapshot, nullable line and legacy immutable evidence integration."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import uuid

import asyncpg
from fastapi import FastAPI
import httpx
from pydantic import ValidationError

from app import database
from app.database import apply_migrations
from app.expert_statistics import QUOTE_VALID_SQL
from app.result_ledger import project_settlements
from app.review_state import quote_context
from app.orders import eligible_signal
from app.routes import internal, reviews
from app.routes.internal import FixtureBatch, FixtureInput, router


def quote(index=0, **updates):
    now=datetime.now(timezone.utc).isoformat()
    return {'market_id':f'm{index}','market_name':'让球','kind':'handicap','line':'0',
            'selection':'home','price':'1.95','source_updated_at':now,'received_at':now, **updates}


def fixture(**updates):
    now=datetime.now(timezone.utc).isoformat()
    return {'id':'f','source_id':'f','sport':'football','competition_name':'League','home_team':'Alpha',
            'away_team':'Beta','phase':'scheduled','kickoff_at':now,'received_at':now,
            'source_updated_at':now,'markets_complete':True, **updates}


def chunked(count=501, **updates):
    quotes=[quote(i) for i in range(count)]
    chunks=[quotes[i:i+500] for i in range(0,count,500)]
    return fixture(market_chunks=chunks,market_chunk_count=len(chunks),market_quote_count=count,**updates)


class SnapshotValidationTests(unittest.TestCase):
    def test_chunk_count_total_duplicate_and_mixed_contracts_fail_before_writes(self):
        good=chunked()
        self.assertEqual(501,len(FixtureInput.model_validate(good).all_markets()))
        malformed=[good|{'market_chunk_count':3},good|{'market_quote_count':502},good|{'markets':[]},
                   good|{'market_chunks':good['market_chunks'][:1]},good|{'market_chunks':[[]]},
                   good|{'market_chunk_count':True},good|{'market_chunks':[good['market_chunks'][0]+[quote(500)]]}]
        duplicated=copy.deepcopy(good)
        duplicated['market_chunks'][1][0]=copy.deepcopy(duplicated['market_chunks'][0][0])
        malformed.append(duplicated)
        for payload in malformed:
            with self.subTest(keys=list(payload)), self.assertRaises(ValidationError):
                FixtureInput.model_validate(payload)
        with self.assertRaises(ValidationError):
            FixtureBatch.model_validate({'items':[chunked(1500),chunked(501,id='g',source_id='g')]})
        self.assertEqual(2000,len(FixtureBatch.model_validate({'items':[chunked(2000)]}).items[0].all_markets()))
        self.assertEqual([],FixtureInput.model_validate(fixture(markets=[])).all_markets())


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated database')
class SnapshotAtomicTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.admin_url=os.environ['TEST_DATABASE_ADMIN_URL']
        self.dbname='test_snapshot_'+uuid.uuid4().hex
        self.admin=await asyncpg.connect(self.admin_url)
        await self.admin.execute(f'CREATE DATABASE "{self.dbname}"')
        async def codecs(conn):
            await conn.set_type_codec('jsonb',schema='pg_catalog',encoder=json.dumps,decoder=json.loads)
        self.pool=await asyncpg.create_pool(self.admin_url.rsplit('/',1)[0]+'/'+self.dbname,
                                           min_size=1,max_size=3,init=codecs)
        async with self.pool.acquire() as conn:
            await apply_migrations(conn)
        database.pool=self.pool
        app=FastAPI()
        app.state.settings=SimpleNamespace(collector_token='isolated-only',mcp_internal_token='isolated-only',quote_max_age_seconds=60)
        app.include_router(router)
        app.include_router(reviews.router)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False),
                                     base_url='http://test',headers={'Authorization':'Bearer isolated-only'})

    async def asyncTearDown(self):
        await self.client.aclose()
        database.pool=None
        await self.pool.close()
        await self.admin.execute(f'DROP DATABASE "{self.dbname}"')
        await self.admin.close()

    async def post(self,payload):
        return await self.client.post('/internal/collector/fixtures',json={'items':[payload]})

    async def test_full_501_snapshot_closes_only_after_all_chunks_and_replay_keeps_ids(self):
        self.assertEqual(200,(await self.post(fixture(markets=[quote(999)]))).status_code)
        payload=chunked()
        response=await self.post(payload)
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual(501,await self.pool.fetchval("SELECT count(*) FROM v2_current_quotes WHERE status='active'"))
        self.assertEqual('closed',await self.pool.fetchval("SELECT status FROM v2_current_quotes WHERE market_id='m999'"))
        before=await self.pool.fetch('SELECT id,market_id FROM v2_current_quotes ORDER BY market_id')
        self.assertEqual(200,(await self.post(payload)).status_code)
        self.assertEqual(before,await self.pool.fetch('SELECT id,market_id FROM v2_current_quotes ORDER BY market_id'))

    async def test_full_2000_snapshot_fits_database_five_second_statement_budget(self):
        await self.pool.execute("ALTER DATABASE "+self.dbname+" SET statement_timeout='5s'")
        await self.pool.expire_connections()
        response=await self.post(chunked(2000))
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual(2000,await self.pool.fetchval("SELECT count(*) FROM v2_current_quotes WHERE status='active'"))

    async def test_body_stream_deadline_precedes_validation_or_database_access(self):
        timeout=asyncio.timeout
        async def slow_body():
            yield b'{'
            await asyncio.sleep(.05)
            yield b'}'
        with patch.object(internal.asyncio,'timeout',side_effect=lambda _:timeout(.01)):
            response=await self.client.post('/internal/collector/fixtures',content=slow_body(),headers={'content-type':'application/json'})
        self.assertEqual(408,response.status_code,response.text)
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_fixtures'))

    async def test_truncated_conflicting_duplicate_and_oversized_bodies_preserve_snapshot(self):
        self.assertEqual(200,(await self.post(fixture(markets=[quote(999)]))).status_code)
        malformed=chunked()
        malformed['market_chunks'].pop()
        response=await self.post(malformed)
        self.assertEqual(422,response.status_code,response.text)
        duplicate=fixture(markets=[quote(0,kind='winner',line=None),quote(0,kind='winner',line=0)])
        self.assertEqual(422,(await self.post(duplicate)).status_code)
        async def oversized():
            for _ in range(5):
                yield b'x'*(1024*1024)
        response=await self.client.post('/internal/collector/fixtures',content=oversized(),headers={'content-type':'application/json'})
        self.assertEqual(413,response.status_code,response.text)
        rows=await self.pool.fetch('SELECT market_id,status FROM v2_current_quotes')
        self.assertEqual([{'market_id':'m999','status':'active'}],[dict(row) for row in rows])

    async def test_last_chunk_database_error_rolls_back_fixture_quotes_and_closure(self):
        self.assertEqual(200,(await self.post(fixture(markets=[quote(999)]))).status_code)
        await self.pool.execute("CREATE FUNCTION fail_last_chunk() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.market_id='m500' THEN RAISE EXCEPTION 'isolated injected fault'; END IF; RETURN NEW; END $$; CREATE TRIGGER snapshot_fault BEFORE INSERT ON v2_current_quotes FOR EACH ROW EXECUTE FUNCTION fail_last_chunk()")
        response=await self.post(chunked(home_team='Changed'))
        self.assertEqual(500,response.status_code)
        self.assertEqual('Alpha',await self.pool.fetchval("SELECT home_team FROM v2_fixtures WHERE id='f'"))
        self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_current_quotes'))
        self.assertEqual('active',await self.pool.fetchval('SELECT status FROM v2_current_quotes'))

    async def test_legacy_winner_quote_identity_and_immutable_signal_evidence_survive_null(self):
        self.assertEqual(200,(await self.post(fixture(markets=[]))).status_code)
        now=datetime.now(timezone.utc)
        quote_id=uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,market_kind,line,selection,price,source_updated_at,score_basis) VALUES($1,'f','1','独赢','winner',0,'home',1.95,$2,'full_period')",quote_id,now-timedelta(seconds=1))
        source,expert,message,signal=[uuid.uuid4() for _ in range(4)]
        await self.pool.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,'chat','source')",source)
        await self.pool.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,'author','Expert')",expert)
        await self.pool.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,posted_at) VALUES($1,$2,$3,'1',now())",message,source,expert)
        await self.pool.execute("INSERT INTO v2_expert_signals(id,message_id,expert_id,fixture_id,phase,market_id,market_name,market_kind,line,selection,recommendation_price,quote_time,confidence,score_basis) VALUES($1,$2,$3,'f','scheduled','1','独赢','winner',0,'home',1.95,$4,.9,'full_period')",signal,message,expert,now)
        await self.pool.execute("INSERT INTO v2_quote_evidence(id,fixture_id,market_id,market_name,line,selection,price,evidence_kind,reference_id,source_updated_at,evidence) VALUES($1,'f','1','独赢',0,'home',1.95,'expert_signal',$2,$3,$4)",uuid.uuid4(),signal,now,{'quote_id':str(quote_id),'score_basis':'full_period'})
        facts={'status':'confirmed','home':2,'away':1}
        await self.pool.execute("INSERT INTO v2_fixture_results(fixture_id,version,provider_reference,content_hash,status,home_score,away_score,source_updated_at,evidence) VALUES('f',1,'isolated-result','isolated-hash','confirmed',2,1,now(),'{}')")
        async with self.pool.acquire() as conn:
            await project_settlements(conn,{'id':'f','sport':'football'},1,facts)
        old_calculation=await self.pool.fetchval('SELECT calculation FROM v2_signal_settlements WHERE signal_id=$1',signal)
        response=await self.post(fixture(markets=[quote(0,market_id='1',kind='winner',line=None,score_basis='full_period')]))
        self.assertEqual(200,response.status_code,response.text)
        current=await self.pool.fetchrow("SELECT id,line,status FROM v2_current_quotes WHERE market_id='1'")
        self.assertEqual((quote_id,None,'active'),tuple(current.values()))
        async with self.pool.acquire() as conn:
            await project_settlements(conn,{'id':'f','sport':'football'},1,facts)
        self.assertEqual(old_calculation,await self.pool.fetchval('SELECT calculation FROM v2_signal_settlements WHERE signal_id=$1',signal))
        self.assertEqual(Decimal(0),await self.pool.fetchval('SELECT line FROM v2_expert_signals WHERE id=$1',signal))
        self.assertTrue(await self.pool.fetchval('SELECT '+QUOTE_VALID_SQL+' FROM v2_expert_signals s WHERE s.id=$1',signal))
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.pool.execute("INSERT INTO v2_expert_signals(id,message_id,expert_id,fixture_id,phase,market_id,market_name,market_kind,line,selection,recommendation_price,quote_time,confidence) SELECT $1,message_id,expert_id,fixture_id,phase,market_id,market_name,market_kind,NULL,selection,recommendation_price,quote_time,confidence FROM v2_expert_signals WHERE id=$2",uuid.uuid4(),signal)

    async def test_database_rejects_missing_numeric_and_untyped_null_evidence(self):
        self.assertEqual(200,(await self.post(fixture(markets=[]))).status_code)
        for kind,line in [('handicap',None),('total',None),('winner',Decimal('.25')),('handicap',Decimal('NaN'))]:
            with self.subTest(kind=kind,line=str(line)),self.assertRaises(asyncpg.CheckViolationError):
                await self.pool.execute("INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,market_kind,line,selection,price,source_updated_at) VALUES($1,'f','m','Market',$2,$3,'home',1.95,now())",uuid.uuid4(),kind,line)
        with self.assertRaises(asyncpg.CheckViolationError):
            await self.pool.execute("INSERT INTO v2_quote_evidence(id,fixture_id,market_id,market_name,line,selection,price,evidence_kind,reference_id,source_updated_at) VALUES($1,'f','1','独赢',NULL,'home',1.95,'expert_signal',$2,now())",uuid.uuid4(),uuid.uuid4())
        await self.pool.execute("INSERT INTO v2_quote_evidence(id,fixture_id,market_id,market_name,line,selection,price,evidence_kind,reference_id,source_updated_at,evidence) VALUES($1,'f','1','独赢',NULL,'home',1.95,'expert_signal',$2,now(),'{\"market_kind\":\"winner\"}')",uuid.uuid4(),uuid.uuid4())

    async def test_migration_preflight_refuses_incompatible_old_rows_without_rewriting(self):
        self.assertEqual(200,(await self.post(fixture(markets=[]))).status_code)
        name='018_explicit_market_line.sql'
        script=(Path(database.MIGRATIONS)/name).read_text()
        async with self.pool.acquire() as conn:
            for duplicate in (False,True):
                transaction=conn.transaction()
                await transaction.start()
                try:
                    await conn.execute('ALTER TABLE v2_current_quotes DROP CONSTRAINT v2_quotes_explicit_line_check')
                    if duplicate:
                        await conn.execute('DROP INDEX v2_quotes_reference_unique')
                        for line in (None,Decimal(0)):
                            await conn.execute("INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,market_kind,line,selection,price,source_updated_at) VALUES($1,'f','1','独赢','winner',$2,'home',1.95,now())",uuid.uuid4(),line)
                    else:
                        await conn.execute("INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,market_kind,line,selection,price,source_updated_at) VALUES($1,'f','1','独赢','winner',.25,'home',1.95,now())",uuid.uuid4())
                    with self.assertRaisesRegex(asyncpg.RaiseError,'explicit_line_preflight_'):
                        await conn.execute(script)
                finally:
                    await transaction.rollback()
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_current_quotes'))

    async def test_new_winner_mcp_signal_statistics_orders_and_settlement_use_explicit_null(self):
        payload=fixture(phase='live',home_score=1,away_score=0,markets=[quote(0,market_id='1',kind='winner',line=None,score_basis='full_period',play_id='1',market_instance_id='m',selection_id='o')])
        self.assertEqual(200,(await self.post(payload)).status_code)
        source,expert,message,job,attempt=[uuid.uuid4() for _ in range(5)]
        await self.pool.execute("INSERT INTO v2_expert_sources(id,platform_chat_id,display_name) VALUES($1,'chat','source')",source)
        await self.pool.execute("INSERT INTO v2_experts(id,platform_sender_id,display_nickname) VALUES($1,'author','Expert')",expert)
        await self.pool.execute("INSERT INTO v2_source_experts(source_id,expert_id) VALUES($1,$2)",source,expert)
        await self.pool.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) VALUES($1,$2,$3,'1','isolated intent',now())",message,source,expert)
        await self.pool.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key,attempt_id,lease_until) VALUES($1,$2,'running',$3,$4,now()+interval '5 minutes')",job,message,str(job),attempt)
        current=await self.pool.fetchrow("SELECT q.*,f.phase,f.home_score,f.away_score,f.half_home_score,f.half_away_score FROM v2_current_quotes q JOIN v2_fixtures f ON f.id=q.fixture_id")
        await self.pool.execute("INSERT INTO v2_review_fixture_bindings(job_id,attempt_id,fixture_id,search_key,evidence) VALUES($1,$2,'f','isolated','{}')",job,attempt)
        await self.pool.execute("INSERT INTO v2_review_market_reads(job_id,attempt_id,quote_id,source_updated_at,price,quote_context) VALUES($1,$2,$3,$4,$5,$6)",job,attempt,current['id'],current['source_updated_at'],current['price'],quote_context(current,current))
        request={'review_id':str(job),'attempt_id':str(attempt),'signals':[{'fixture_id':'f','quote_id':str(current['id']),'evidence_text':'isolated home intent','confidence':.9}]}
        first=await self.client.post('/internal/mcp/record_expert_signals',json=request)
        self.assertEqual(200,first.status_code,first.text)
        accepted=first.json()['accepted'][0]
        self.assertIsNone(accepted['line'])
        repeated=await self.client.post('/internal/mcp/record_expert_signals',json=request)
        self.assertEqual(200,repeated.status_code,repeated.text)
        self.assertTrue(repeated.json()['accepted'][0]['duplicate'])
        self.assertEqual(accepted['signal_id'],repeated.json()['accepted'][0]['signal_id'])
        signal=uuid.UUID(accepted['signal_id'])
        evidence=await self.pool.fetchrow('SELECT line,evidence FROM v2_quote_evidence WHERE reference_id=$1',signal)
        self.assertIsNone(evidence['line'])
        self.assertEqual('winner',evidence['evidence']['market_kind'])
        self.assertTrue(await self.pool.fetchval('SELECT '+QUOTE_VALID_SQL+' FROM v2_expert_signals s WHERE s.id=$1',signal))
        async with self.pool.acquire() as conn:
            self.assertIsNone((await eligible_signal(conn,signal,60))['line'])
            await conn.execute("INSERT INTO v2_fixture_results(fixture_id,version,provider_reference,content_hash,status,home_score,away_score,source_updated_at,evidence) VALUES('f',1,'isolated','isolated','confirmed',2,1,now(),'{}')")
            await project_settlements(conn,{'id':'f','sport':'football'},1,{'status':'confirmed','home':2,'away':1})
        outcome=await self.pool.fetchrow('SELECT outcome,unit_net FROM v2_expert_signals WHERE id=$1',signal)
        self.assertEqual(('win',Decimal('.95')),tuple(outcome.values()))
        self.assertIsNone((await self.pool.fetchval('SELECT calculation FROM v2_signal_settlements WHERE signal_id=$1',signal))['signal']['line'])


if __name__=='__main__':
    unittest.main()
