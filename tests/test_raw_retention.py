"""Storage lifecycle checks against task-owned isolated PostgreSQL only."""
import asyncio
import os
import unittest
import uuid
from datetime import timedelta
from app.raw_retention import sweep, validate_local_target
from app.message_read import message_projection
from app.retention_media import claim_expired_media, acknowledge_expired_media
import test_result_ledger as support


class LocalRetentionGuardTests(unittest.TestCase):
    def test_rejects_remote_targets_and_dsn_overrides(self):
        validate_local_target('postgresql://a:b@local-postgres/football_v2?sslmode=verify-full')
        for value in (
            'postgresql://a:b@remote/football_v2?sslmode=verify-full',
            'postgresql://a:b@local-postgres/other?sslmode=verify-full',
            'postgresql://a:b@local-postgres/football_v2?sslmode=disable',
            'postgresql://a:b@local-postgres/football_v2?sslmode=verify-full&host=remote',
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_local_target(value)

    def test_expired_projection_cannot_issue_media_grants(self):
        result = message_projection({'id': uuid.uuid4(), 'raw_content_expired_at':'expired',
            'raw_text':'should never be returned','media_objects':[{'object_key':'private'}]})
        self.assertTrue(result['raw_content_expired'])
        self.assertEqual([], result['media_objects'])
        self.assertNotIn('should never',repr(result))


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated database')
class RawRetentionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ResultLedgerTests.asyncSetUp
    asyncTearDown = support.ResultLedgerTests.asyncTearDown
    signal = support.ResultLedgerTests.signal

    async def test_old_raw_content_expires_without_mutating_parsed_decisions(self):
        signal = await self.signal()
        before = await self.pool.fetchval('SELECT to_jsonb(s)::text FROM v2_expert_signals s WHERE id=$1',signal)
        key = 'v2/telegram/messages/chat/1/hash'
        await self.pool.execute('UPDATE v2_expert_messages SET media_objects=$1 WHERE id=$2',
            [{'object_key':key,'bytes':12}],self.message)
        # Editing an old message must not extend the three-day original TTL.
        await self.pool.execute("UPDATE v2_expert_messages SET posted_at=$1,edited_at=now() WHERE id=$2",
            self.now-timedelta(days=4),self.message)
        message = await self.pool.fetchrow('SELECT * FROM v2_expert_messages WHERE id=$1',self.message)
        self.assertEqual('', message['raw_text'])
        self.assertEqual([], message['media_objects'])
        self.assertEqual({}, message['reply_context'])
        self.assertIsNotNone(message['raw_content_expired_at'])
        self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_raw_retention_media'))
        async with self.pool.acquire() as conn:
            await sweep(conn)
            again = await sweep(conn)
        self.assertEqual(before,await self.pool.fetchval('SELECT to_jsonb(s)::text FROM v2_expert_signals s WHERE id=$1',signal))
        self.assertTrue(await self.pool.fetchval('SELECT EXISTS(SELECT 1 FROM v2_expert_messages WHERE id=$1)',self.message))
        self.assertEqual(0,again['messages_expired'])
        response = await self.client.get('/api/v1/public/signals')
        self.assertEqual(200,response.status_code)
        self.assertEqual(1,len(response.json()['items']))

    async def test_unreferenced_old_records_deleted_and_current_content_kept(self):
        await self.pool.execute("UPDATE v2_expert_messages SET posted_at=$1 WHERE id=$2",
            self.now-timedelta(days=4),self.message)
        await self.pool.execute("UPDATE v2_fixtures SET kickoff_at=$1 WHERE id='fixture'",
            self.now-timedelta(days=4))
        async with self.pool.acquire() as conn:
            await sweep(conn)
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_messages'))
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_fixtures'))
        fresh=uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) "
            "VALUES($1,$2,$3,'new','current original',now())",fresh,self.source,self.expert)
        async with self.pool.acquire() as conn:
            await sweep(conn)
        self.assertEqual('current original',await self.pool.fetchval('SELECT raw_text FROM v2_expert_messages WHERE id=$1',fresh))

    async def test_old_fixture_becomes_small_decision_context_and_leaves_current_lists(self):
        await self.signal()
        await self.pool.execute("UPDATE v2_fixtures SET home_aliases=ARRAY['raw alias'],clock_minute=99")
        await self.pool.execute("UPDATE v2_fixtures SET kickoff_at=$1",self.now-timedelta(days=4))
        async with self.pool.acquire() as conn:
            await sweep(conn)
        row=await self.pool.fetchrow('SELECT * FROM v2_fixtures')
        self.assertEqual('Alpha',row['home_team'])
        self.assertEqual([],row['home_aliases'])
        self.assertIsNone(row['clock_minute'])
        self.assertIsNotNone(row['decision_context_only_at'])
        self.assertEqual([], (await self.client.get('/api/v1/public/matches')).json()['items'])
        self.assertEqual('football',(await self.client.get('/api/v1/public/signals')).json()['items'][0]['sport'])

    async def test_expired_jobs_and_late_traces_cannot_restore_raw_content(self):
        await self.signal()
        await self.pool.execute("UPDATE v2_expert_messages SET posted_at=$1 WHERE id=$2",
            self.now-timedelta(days=4),self.message)
        job=uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key) "
            "VALUES($1,$2,'queued','expired-job')",job,self.message)
        row=await self.pool.fetchrow('SELECT state,last_error FROM v2_analysis_jobs WHERE id=$1',job)
        self.assertEqual(('failed','raw_content_expired'),tuple(row))
        await self.pool.execute("INSERT INTO v2_analysis_traces(id,job_id,sequence_no,trace_kind,payload) "
            "VALUES($1,$2,1,'model_output',$3)",uuid.uuid4(),job,{'raw_text':'must not persist'})
        payload=await self.pool.fetchval('SELECT payload FROM v2_analysis_traces WHERE job_id=$1',job)
        self.assertEqual({'retention':'raw_content_expired'},payload)

    async def test_expired_quote_projection_cannot_be_rehydrated(self):
        await self.signal()
        await self.pool.execute("UPDATE v2_fixtures SET kickoff_at=$1",self.now-timedelta(days=4))
        await self.pool.execute("INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,period,line,selection,price,status,source_updated_at) "
            "VALUES($1,'fixture','total','大小','full_time',2.5,'over',1.9,'active',now())",uuid.uuid4())
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_current_quotes'))

    async def test_media_retirement_checks_source_ownership_leases_and_live_references(self):
        owner,other=uuid.uuid4(),uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_source_checkpoints(source_id,collector_owner,lease_until) "
            "VALUES($1,$2,now()+interval '90 seconds')",self.source,owner)
        key='v2/telegram/messages/chat/old/hash'
        await self.pool.execute('INSERT INTO v2_raw_retention_media(object_key,source_id) VALUES($1,$2)',key,self.source)
        async with self.pool.acquire() as conn:
            self.assertEqual([],await claim_expired_media(conn,other,100))
            await self.pool.execute('UPDATE v2_expert_messages SET media_objects=$1 WHERE id=$2',
                [{'object_key':key}],self.message)
            self.assertEqual([],await claim_expired_media(conn,owner,100))
            await self.pool.execute("UPDATE v2_expert_messages SET media_objects='[]'::jsonb WHERE id=$1",self.message)
            self.assertEqual(1,len(await claim_expired_media(conn,owner,100)))
            self.assertEqual([],await claim_expired_media(conn,owner,100))
            self.assertEqual(0,await acknowledge_expired_media(conn,other,[key]))
            self.assertEqual(1,await acknowledge_expired_media(conn,owner,[key]))
