"""Version conflict diagnostics and persistence use isolated PostgreSQL only."""
import unittest
from datetime import datetime, timedelta, timezone
import time
from unittest.mock import AsyncMock
import httpx

import test_telegram_api as support
import test_telegram_collector as collector_support
from app.routes.telegram import log_version_conflict
from collector.telegram import TelegramCollector, SYNC_HISTORY_MAX_MESSAGES
from collector.telegram_backend import TelegramBackend


class ConflictLogTests(unittest.TestCase):
    def test_only_schema_field_names_are_logged(self):
        with self.assertLogs('platform.api', level='WARNING') as captured:
            log_version_conflict({'raw_text': '原文秘密', 'sender_id': '私密编号', 'extra-private': 1},
                                 {'raw_text': '其他秘密', 'sender_id': '另一编号'}, path='live')
        output = '\n'.join(captured.output)
        self.assertIn('fields=raw_text,sender_id', output)
        for value in ['原文秘密', '其他秘密', '私密编号', '另一编号', 'extra-private']:
            self.assertNotIn(value, output)


class ConflictTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_legitimate_claim_conflict_is_classified_and_not_retried(self):
        calls = []
        def reject(request):
            calls.append(request)
            return httpx.Response(409,json={'detail':'Telegram reconciliation claim is no longer active'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
            backend = TelegramBackend(client,'http://isolated.invalid')
            with self.assertLogs('platform.telegram',level='WARNING') as captured:
                with self.assertRaises(httpx.HTTPStatusError):
                    await backend.event({'chat_id':'-1001','platform_message_id':'11'})
        self.assertEqual(1,len(calls))
        self.assertIn('reason=reconciliation_claim_inactive','\n'.join(captured.output))


@unittest.skipUnless(support.TEST_URL, 'requires explicit temporary Telegram PostgreSQL')
class VersionConflictTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.TelegramApiTests.asyncSetUp
    asyncTearDown = support.TelegramApiTests.asyncTearDown
    event = support.TelegramApiTests.event
    post = support.TelegramApiTests.post

    async def test_live_conflict_reports_changed_fields_but_keeps_immutable_content(self):
        body = self.event()
        self.assertEqual(200, (await self.post(body)).status_code)
        with self.assertLogs('platform.api', level='WARNING') as captured:
            rejected = await self.post(body | {'raw_text': '不能覆盖的私密修改'})
        self.assertEqual(409, rejected.status_code)
        output = '\n'.join(captured.output)
        self.assertIn('path=live fields=raw_text', output)
        self.assertNotIn(body['raw_text'], output)
        self.assertNotIn('不能覆盖的私密修改', output)
        self.assertEqual(body['raw_text'], await self.pool.fetchval('SELECT raw_text FROM v2_expert_messages'))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_jobs'))

    async def test_identical_retry_has_one_event_and_no_conflict_log(self):
        body = self.event()
        self.assertEqual(200, (await self.post(body)).status_code)
        with self.assertNoLogs('platform.api', level='WARNING'):
            self.assertEqual(200, (await self.post(body)).status_code)
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_telegram_events'))
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_jobs'))

    async def test_expired_replay_is_terminal_and_cannot_recreate_raw_content_or_jobs(self):
        body = self.event(days_ago=4)
        for _ in range(2):
            response = await self.post(body)
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual('expired', response.json()['state'])
        for table in ['v2_expert_messages', 'v2_telegram_events', 'v2_analysis_jobs']:
            self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM '+table))

    async def test_existing_expired_version_cannot_be_restored_by_fresh_posted_at(self):
        body = self.event()
        response = await self.post(body)
        self.assertEqual(200, response.status_code)
        await self.pool.execute("UPDATE v2_telegram_events SET content=jsonb_set(content,'{posted_at}',"
            "to_jsonb((now()-interval '4 days')::text))")
        before = await self.pool.fetchrow('SELECT * FROM v2_telegram_events')
        self.assertEqual({}, before['content'])
        response = await self.post(body)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('expired', response.json()['state'])
        after = await self.pool.fetchrow('SELECT * FROM v2_telegram_events')
        self.assertEqual(before, after)
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_analysis_jobs'))

    async def test_expired_input_cannot_bypass_lease_or_trusted_author(self):
        body = self.event(days_ago=4)
        self.assertEqual(403, (await self.post(body | {'sender_id': '999'})).status_code)
        await self.pool.execute("UPDATE v2_source_checkpoints SET lease_until=now()-interval '1 second'")
        self.assertEqual(409, (await self.post(body)).status_code)
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_telegram_events'))

    async def test_reconciliation_expired_input_is_acknowledged_without_replacing_original(self):
        body = self.event(days_ago=2)
        self.assertEqual(200,(await self.post(body)).status_code)
        await self.pool.execute("UPDATE v2_telegram_events SET next_check_at=now()-interval '1 minute'")
        claimed=await self.client.post('/internal/telegram/observed',json={'chat_id':'-1001',
            'owner':str(self.owner),'reconciliation_protocol':'claim-ack-v1','limit':1})
        self.assertEqual(200,claimed.status_code,claimed.text)
        item,=claimed.json()['items']
        reference={'event_id':item['id'],'version_key':item['version_key'],'claim_id':item['claim_id']}
        response=await self.post(body|{'posted_at':(self.event_time-timedelta(days=4)).isoformat(),
                                      'reconciliation':reference})
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual('expired',response.json()['state'])
        self.assertTrue(response.json()['reconciliation_acknowledged'])
        self.assertEqual(body['raw_text'],await self.pool.fetchval('SELECT raw_text FROM v2_expert_messages'))
        self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_telegram_events'))
        self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_analysis_jobs'))


class RetainedHistoryCollectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = collector_support.Backend()
        self.client = collector_support.Client()
        self.collector = TelegramCollector(collector_support.CONFIG, background_read_interval=0, backend=self.backend,
                                           client_factory=lambda *_: self.client)
        self.collector.client = self.client
        self.source = self.backend.source_items[0]
        self.collector.sources = {'-1001': self.source}
        self.collector._refresh_directory = AsyncMock()
        self.collector._recover_recent = AsyncMock(return_value={'state': 'caught_up'})

    async def asyncTearDown(self):
        await self.collector.disconnect()

    async def test_archive_seeks_retained_window_and_commits_fresh_after_old_album(self):
        old = collector_support.Message(2, media=True, grouped=44)
        old.date = datetime.now(timezone.utc)-timedelta(days=1000)
        old.get_sender = AsyncMock(side_effect=AssertionError('Expired post must not be fetched'))
        fresh = collector_support.Message(100)
        fresh.date = datetime.now(timezone.utc)-timedelta(hours=1)
        self.client.messages['-1001'] = [old, fresh]
        self.backend.positions['-1001'] = 1
        await self.collector._sync_source('-1001', self.source)
        self.assertEqual(['100'], [p['platform_message_id'] for p in self.backend.calls])
        self.assertEqual(100, self.backend.positions['-1001'])
        old.get_sender.assert_not_awaited()

    async def test_terminal_expired_receipt_does_not_start_media_transfer(self):
        self.backend.event = AsyncMock(return_value={'persisted': True, 'state': 'expired'})
        self.collector._complete_pending_media = AsyncMock(side_effect=AssertionError('No expired media'))
        result = await self.collector._process_group('-1001', self.source, [collector_support.Message(4, media=True)])
        self.assertEqual('expired', result['state'])
        self.collector._complete_pending_media.assert_not_awaited()

    async def test_retention_seek_failure_does_not_advance_checkpoint_or_cache(self):
        self.backend.positions['-1001'] = 1
        async def broken(*args,**kwargs):
            raise ConnectionError('isolated history lookup failure')
            yield
        self.client.iter_messages = broken
        with self.assertRaises(ConnectionError):
            await self.collector._sync_source('-1001',self.source)
        self.assertEqual(1,self.backend.positions['-1001'])
        self.assertEqual({},self.collector.retained_history_floor)

    async def test_empty_retained_window_is_cached_without_fabricating_checkpoint(self):
        self.backend.positions['-1001'] = 100
        for _ in range(2):
            result = await self.collector._sync_source('-1001',self.source)
            self.assertEqual('caught_up',result['state'])
        self.assertEqual([1],self.client.query_limits)
        self.assertEqual(100,self.backend.positions['-1001'])
        self.assertEqual([],self.backend.calls)

    async def test_cached_floor_does_not_remove_expired_history_slice_budget(self):
        self.backend.positions['-1001'] = 1
        self.collector.retained_history_floor['-1001'] = (self.client,time.monotonic()+300,2)
        messages = [collector_support.Message(i) for i in range(2,202)]
        for item in messages:item.date=datetime.now(timezone.utc)-timedelta(days=4)
        self.client.messages['-1001'] = messages
        result = await self.collector._sync_source('-1001',self.source)
        self.assertEqual('catching_up',result['state'])
        self.assertEqual(SYNC_HISTORY_MAX_MESSAGES,result['observed'])
        self.assertEqual(1+SYNC_HISTORY_MAX_MESSAGES,self.backend.positions['-1001'])
        self.assertEqual([],self.backend.calls)

    async def test_personal_author_backfill_also_stops_at_original_72_hours(self):
        now=datetime.now(timezone.utc)
        self.source['author_backfills']=[{'sender_id':'7','cursor':101,'until_id':100,
            'since':(now-timedelta(days=7)).isoformat()}]
        fresh=collector_support.Message(100);fresh.date=now-timedelta(days=2)
        old=collector_support.Message(99);old.date=now-timedelta(days=4)
        self.client.messages['-1001']=[old,fresh]
        result=await self.collector._sync_author_backfill('-1001',self.source)
        self.assertEqual('complete',result['state'])
        self.assertEqual(['100'],[p['platform_message_id']for p in self.backend.calls])
        self.assertEqual([],self.source['author_backfills'])

    async def test_retention_floor_is_bound_to_the_current_client(self):
        entity=await self.collector._entity('-1001')
        self.client.messages['-1001']=[collector_support.Message(100)]
        self.assertEqual(100,await self.collector._retained_history_start('-1001',entity))
        replacement=collector_support.Client({'-1001':[collector_support.Message(200)]})
        self.collector.client=replacement
        self.assertEqual(200,await self.collector._retained_history_start('-1001',entity))
        self.assertEqual([1],replacement.query_limits)
