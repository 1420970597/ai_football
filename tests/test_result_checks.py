import asyncio
from datetime import datetime, timedelta, timezone
import os
import unittest
import uuid

from app.routes import result_checks
from app.routes.internal import FixtureInput
from app.ingestion import ingest_fixture_batch
from collector.leyu_results import normalize_result
import test_result_ledger as support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires task-owned isolated PostgreSQL')
class ResultCheckTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ResultLedgerTests.asyncSetUp
    asyncTearDown=support.ResultLedgerTests.asyncTearDown
    signal=support.ResultLedgerTests.signal
    result=support.ResultLedgerTests.result
    submit=support.ResultLedgerTests.submit

    async def prepare(self):
        self.app.include_router(result_checks.router)
        await self.pool.execute("UPDATE v2_fixtures SET source_id='leyu:123',kickoff_at=$1,phase='finished'",self.now-timedelta(hours=3))
        await self.signal(line='1.5')
        return await self.claim()

    async def post(self,path,body,auth=True):
        return await self.client.post('/internal/collector/results/'+path,json=body,
                                      headers={'Authorization':'Bearer isolated-collector'} if auth else {})

    async def claim(self):
        response=await self.post('claim',{'limit':4})
        self.assertEqual(200,response.status_code,response.text)
        return response.json()['items']

    def observation(self,target,home=2,at=None):
        now=at or datetime.now(timezone.utc)
        detail={'mid':'123','csid':'1','mgt':target['kickoff_at'],'mhn':'Alpha','man':'Beta',
                'ms':3,'mmp':'999','mft':0,'msc':[f'S1|{home}:0','S2|0:0',f'S3|{home}:0']}
        return normalize_result(detail,target,requested_at=now,observed_at=now)

    async def due(self):
        await self.pool.execute("UPDATE v2_result_checks SET next_check_at=now()-interval '1 second'")

    async def test_actual_result_settles_once_and_replay_preserves_immutable_receipt(self):
        target,=await self.prepare()
        body=self.observation(target)
        response=await self.post('observations',body)
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual(1,response.json()['settled'])
        original=await self.pool.fetchrow('SELECT outcome,unit_net,settled_at FROM v2_expert_signals')
        self.assertEqual('win',original['outcome'])
        fact=await self.pool.fetchrow('SELECT * FROM v2_fixture_results')
        self.assertIsNone(fact['source_updated_at'])
        self.assertIsNotNone(fact['observed_at'])
        self.assertEqual([],await self.claim())
        self.assertEqual(409,(await self.post('observations',body)).status_code)
        await self.due()
        replay,=await self.claim()
        newer=self.observation(replay)
        newer['evidence']['decoded_response_sha256']='f'*64
        response=await self.post('observations',newer)
        self.assertEqual(200,response.status_code,response.text)
        self.assertTrue(response.json()['duplicate'])
        self.assertEqual(fact,await self.pool.fetchrow('SELECT * FROM v2_fixture_results'))
        self.assertEqual(original,await self.pool.fetchrow('SELECT outcome,unit_net,settled_at FROM v2_expert_signals'))
        self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_signal_settlements'))

    async def test_correction_versions_retain_original_and_late_signals_recover(self):
        target,=await self.prepare()
        self.assertEqual(200,(await self.post('observations',self.observation(target))).status_code)
        await self.due()
        second,=await self.claim()
        corrected=await self.post('observations',self.observation(second,home=0))
        self.assertEqual(200,corrected.status_code,corrected.text)
        self.assertEqual(2,corrected.json()['version'])
        self.assertEqual(['win','loss'],[r['outcome'] for r in await self.pool.fetch('SELECT outcome FROM v2_signal_settlements ORDER BY result_version')])
        late=await self.signal(selection='under',line='1.5')
        await self.due()
        third,=await self.claim()
        response=await self.post('observations',self.observation(third,home=0))
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual('win',await self.pool.fetchval('SELECT outcome FROM v2_expert_signals WHERE id=$1',late))
        self.assertEqual(2,await self.pool.fetchval('SELECT count(*) FROM v2_fixture_results'))

    async def test_expired_wrong_and_cross_fixture_leases_cannot_write(self):
        target,=await self.prepare()
        body=self.observation(target)
        for changes in ({'lease_id':str(uuid.uuid4())},{'fixture_id':'other'}):
            self.assertEqual(409,(await self.post('observations',body|changes)).status_code)
        bad={**body,'evidence':body['evidence']|{'mid':'999'}}
        self.assertEqual(422,(await self.post('observations',bad)).status_code)
        self.assertEqual(401,(await self.post('observations',body,auth=False)).status_code)
        await self.pool.execute("UPDATE v2_result_checks SET lease_until=now()-interval '1 second'")
        self.assertEqual(409,(await self.post('observations',body)).status_code)
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_fixture_results'))
        new,=await self.claim()
        self.assertNotEqual(target['lease_id'],new['lease_id'])
        self.assertEqual(200,(await self.post('observations',self.observation(new))).status_code)

    async def test_invalid_period_proof_clock_and_score_never_settle(self):
        target,=await self.prepare()
        body=self.observation(target)
        for proof in ({'period':'6'},{'state':'1'},{'second_half':'9:9'},{'decoded_response_sha256':'invalid'}):
            self.assertEqual(422,(await self.post('observations',body|{'evidence':body['evidence']|proof})).status_code)
        for changes in ({'home':3},{'source_updated_at':self.now.isoformat()},
                        {'requested_at':(self.now-timedelta(minutes=5)).isoformat()},
                        {'observed_at':(self.now+timedelta(seconds=30)).isoformat()}):
            self.assertEqual(422,(await self.post('observations',body|changes)).status_code)
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_fixture_results'))

    async def test_concurrent_claims_exclude_live_and_unrelated_fixtures(self):
        target,=await self.prepare()
        self.assertEqual([],await self.claim())
        await self.pool.execute("UPDATE v2_result_checks SET lease_until=now()-interval '1 second'")
        claims=await asyncio.gather(self.claim(),self.claim())
        self.assertEqual(1,sum(map(len,claims)))
        await self.pool.execute("UPDATE v2_result_checks SET lease_until=NULL")
        await self.pool.execute("UPDATE v2_fixtures SET phase='live',kickoff_at=now()-interval '10 minutes'")
        self.assertEqual([],await self.claim())
        self.assertEqual(422,(await self.post('claim',{'limit':5})).status_code)
        self.assertEqual(401,(await self.post('claim',{},auth=False)).status_code)

    async def test_nonfinal_and_failure_backoff_do_not_create_false_results(self):
        target,=await self.prepare()
        response=await self.post('finish',{k:target[k] for k in ('fixture_id','lease_id')}|{'reason':'result_not_final'})
        self.assertEqual(200,response.status_code,response.text)
        row=await self.pool.fetchrow('SELECT * FROM v2_result_checks')
        self.assertEqual(0,row['failures'])
        self.assertGreater(row['next_check_at'],self.now+timedelta(seconds=20))
        self.assertEqual('pending',await self.pool.fetchval('SELECT outcome FROM v2_expert_signals'))
        await self.due()
        next_target,=await self.claim()
        response=await self.post('finish',{k:next_target[k] for k in ('fixture_id','lease_id')}|{'reason':'upstream_timeout'})
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual(1,await self.pool.fetchval('SELECT failures FROM v2_result_checks'))

    async def test_unknown_clock_cannot_overwrite_source_revision_and_utc_replay_works(self):
        target,=await self.prepare()
        body=self.observation(target)
        body['observed_at']=datetime.fromisoformat(body['observed_at']).astimezone(timezone(timedelta(hours=8))).isoformat()
        self.assertEqual(200,(await self.post('observations',body)).status_code)
        rebuilt=await self.client.post('/internal/collector/results/rebuild',json={'fixture_ids':['fixture']},headers={'Authorization':'Bearer isolated-collector'})
        self.assertEqual(200,rebuilt.status_code,rebuilt.text)
        known=await self.submit(self.result(home=0,away=0,source_updated_at=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()))
        self.assertEqual(200,known.status_code,known.text)
        await self.due()
        new,=await self.claim()
        response=await self.post('observations',self.observation(new))
        self.assertEqual(409,response.status_code,response.text)
        self.assertEqual('loss',await self.pool.fetchval('SELECT outcome FROM v2_expert_signals'))

    async def test_later_scoreboard_snapshot_cannot_erase_confirmed_result(self):
        target,=await self.prepare()
        self.assertEqual(200,(await self.post('observations',self.observation(target))).status_code)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                item=FixtureInput(id='fixture',source_id='leyu:123',sport='football',competition_name='League',
                                  home_team='Alpha',away_team='Beta',kickoff_at=target['kickoff_at'],phase='finished',
                                  source_updated_at=None,received_at=datetime.now(timezone.utc),source_time_quality='unknown')
                await ingest_fixture_batch(conn,[item])
        self.assertEqual((2,0),tuple(await self.pool.fetchrow('SELECT home_score,away_score FROM v2_fixtures')))
        self.assertEqual('win',await self.pool.fetchval('SELECT outcome FROM v2_expert_signals'))
