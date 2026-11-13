"""Broadcast identity discovery and durable recovery; fake Telegram, isolated PG."""
import asyncio
from datetime import datetime, timedelta, timezone
import unittest
import uuid
from unittest.mock import AsyncMock, patch

from telethon import utils
from telethon.errors import ChatAdminRequiredError
from telethon.tl.types import Channel, ChatPhotoEmpty

from collector.telegram import TelegramCollector
from collector.telegram_common import _trusted_message_author
from app.routes import admin
from app.routes.telegram import EventInput, review_job_metadata
import test_telegram_collector as collector_fixture
import test_telegram_directory as directory_fixture
import test_telegram_api as api_fixture


class ChannelIdentityCollectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = collector_fixture.Backend()
        self.client = directory_fixture.DirectoryClient()
        self.entity = Channel(id=91, title='频道发布身份', photo=ChatPhotoEmpty(),
            date=datetime.now(timezone.utc), broadcast=True, megagroup=False, access_hash=123)
        self.chat_id = str(utils.get_peer_id(self.entity))
        self.client.entities[self.chat_id] = self.entity
        self.source = {'chat_id':self.chat_id, 'display_name':'频道显示名',
                       'trusted_sender_ids':[self.chat_id], 'allow_media':True}
        self.backend.source_items = [self.source]
        self.collector = TelegramCollector(collector_fixture.CONFIG, background_read_interval=0, backend=self.backend,
                                           client_factory=lambda *_:self.client)
        self.collector.client = self.client
        self.collector.sources = {self.chat_id:self.source}

    async def asyncTearDown(self):
        await self.collector.disconnect()

    def message(self, identifier, *, sender=None, post=True, hours_ago=1):
        message = collector_fixture.Message(identifier, sender=sender)
        message.post = post
        message.date = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        message.get_sender = AsyncMock(side_effect=AssertionError('No personal author lookup'))
        return message

    def backfill(self, until=100, since_hours=72):
        self.source['author_backfills'] = [{'sender_id':self.chat_id, 'cursor':until+1,
            'until_id':until, 'since':(datetime.now(timezone.utc)-timedelta(hours=since_hours)).isoformat()}]
        self.backend.positions[self.chat_id] = until+100

    async def test_broadcast_directory_offers_self_identity_without_admin_rpc(self):
        self.client.error = ChatAdminRequiredError(None)
        result = await self.collector._admins({'chat_id':self.chat_id})
        self.assertEqual('ready', result['state'])
        self.assertEqual([(self.chat_id, 'channel')],
            [(item['sender_id'],item['admin_kind']) for item in result['items']])
        self.assertFalse(result['items'][0]['individual_author_resolved'])
        self.assertFalse(result['trust_automatically_granted'])
        self.assertIsNone(result['next_cursor'])
        self.assertEqual([],self.client.rpc_calls)

    async def test_channel_search_and_cursor_do_not_repeat_or_grant_trust(self):
        for query, cursor, expected in [('频道',None,1),('不匹配',None,0),('', '50',0)]:
            with self.subTest(query=query,cursor=cursor):
                result = await self.collector._admins({'chat_id':self.chat_id,'q':query,'cursor':cursor})
                self.assertEqual(expected,len(result['items']))
        self.assertEqual({},self.backend.events)

    async def test_megagroup_does_not_become_broadcast_identity(self):
        self.entity.megagroup = True
        self.entity.broadcast = True  # Contradictory flags must fail closed.
        result = await self.collector._admins({'chat_id':self.chat_id})
        self.assertEqual([],result['items'])

    async def test_dialog_metadata_distinguishes_broadcast_from_megagroup(self):
        other = directory_fixture.group(92,channel=True)
        self.client.dialogs = [directory_fixture.dialog(self.entity),directory_fixture.dialog(other)]
        result = await self.collector._dialogs({})
        kinds = {row['chat_id']:row['source_kind'] for row in result['items']}
        self.assertEqual('broadcast_channel',kinds[self.chat_id])
        self.assertEqual('supergroup',kinds[str(utils.get_peer_id(other))])

    async def test_self_peer_and_missing_sender_posts_do_not_need_personal_lookup(self):
        for number,sender in enumerate([None,int(self.chat_id)],1):
            message = self.message(number,sender=sender)
            result = await self.collector._process_group(self.chat_id,self.source,[message])
            self.assertTrue(result['persisted'])
            message.get_sender.assert_not_awaited()
        self.assertEqual({self.chat_id},{item['sender_id'] for item in self.backend.calls})

    async def test_verified_signed_broadcast_post_uses_explicit_channel_trust(self):
        self.source['verified_broadcast'] = True
        for identifier,sender in enumerate([7,-100999],100):
            message=self.message(identifier,sender=sender,post=True)
            result=await self.collector._process_group(self.chat_id,self.source,[message])
            self.assertTrue(result['persisted'])
            self.assertEqual(self.chat_id,self.backend.calls[-1]['sender_id'])
            message.get_sender.assert_not_awaited()

    async def test_unverified_group_or_forwarded_message_does_not_gain_signed_post_trust(self):
        for verified,post in [(False,True),(True,False)]:
            self.source['verified_broadcast']=verified
            result=await self.collector._process_group(self.chat_id,self.source,[self.message(201,sender=7,post=post)])
            self.assertEqual('ignored_author',result['state'])
        self.assertEqual([],self.backend.calls)

    async def test_existing_explicit_personal_identity_stays_stable_on_signed_replay(self):
        from collector.telegram_common import _trusted_message_author
        self.source.update(verified_broadcast=True,trusted_sender_ids=[self.chat_id,'7'])
        self.assertEqual(('7',False),_trusted_message_author(self.chat_id,self.source,self.message(202,sender=7,post=True)))

    async def test_missing_sender_group_or_unsigned_untrusted_channel_stays_ignored(self):
        cases = [(self.source,self.message(1,post=False)),
                 (self.source|{'trusted_sender_ids':[]},self.message(2)),
                 (self.source|{'trusted_sender_ids':['7']},self.message(3))]
        for source,message in cases:
            self.assertIsNone(_trusted_message_author(self.chat_id,source,message))
        self.assertEqual({},self.backend.events)

    async def test_channel_backfill_scans_history_and_skips_other_speakers(self):
        self.backfill(until=5)
        self.client.messages[self.chat_id] = [self.message(1),self.message(2,sender=7,post=False),
            self.message(3,sender=int(self.chat_id)),self.message(4),self.message(5)]
        original = self.client.iter_messages
        calls = []
        async def history(entity,**kwargs):
            calls.append(kwargs)
            self.assertNotIn('from_user',kwargs)
            async for message in original(entity,**kwargs):
                yield message
        self.client.iter_messages = history
        with patch('collector.telegram.AUTHOR_BACKFILL_PAGE_SIZE',2):
            for _ in range(3):
                result = await self.collector._sync_author_backfill(self.chat_id,self.source)
        self.assertEqual('complete',result['state'])
        self.assertEqual({'1','3','4','5'},{item['platform_message_id'] for item in self.backend.calls})
        self.assertEqual(105,self.backend.positions[self.chat_id])
        self.assertEqual([6,4,2],[item['max_id'] for item in calls])
        self.assertTrue(self.backend.backfill_updates[-1][-1])

    async def test_channel_backfill_failure_does_not_advance_cursor(self):
        self.backfill(until=3)
        self.client.messages[self.chat_id] = [self.message(3)]
        self.backend.fail_chat = self.chat_id
        with self.assertRaises(ConnectionError):
            await self.collector._sync_author_backfill(self.chat_id,self.source)
        self.assertEqual(4,self.source['author_backfills'][0]['cursor'])
        self.assertEqual([],self.backend.backfill_updates)

    async def test_channel_backfill_never_recovers_raw_posts_older_than_72_hours(self):
        self.backfill(until=3,since_hours=168)
        self.client.messages[self.chat_id] = [self.message(1,hours_ago=73),self.message(2,hours_ago=71)]
        result = await self.collector._sync_author_backfill(self.chat_id,self.source)
        self.assertEqual('complete',result['state'])
        self.assertEqual(['2'],[item['platform_message_id'] for item in self.backend.calls])

    async def test_channel_recent_recovery_reuses_identity_and_durable_version(self):
        message = self.message(9,sender=int(self.chat_id))
        self.client.messages[self.chat_id] = [message]
        for _ in range(2):
            result = await self.collector._recover_recent(self.chat_id,self.source)
            self.assertEqual('caught_up',result['state'])
        self.assertEqual(1,len(self.backend.events))
        message.get_sender.assert_not_awaited()


class ChannelHistoryAdmissionTests(unittest.TestCase):
    def test_only_recent_channel_posts_enter_realtime_queue(self):
        for sender,age,expected in [('-1001',60,'queued'),('-1001',600,'unresolved'),('7',600,'queued')]:
            with self.subTest(sender=sender,age=age):
                when=datetime.now(timezone.utc)-timedelta(seconds=age)
                body=EventInput(chat_id='-1001',owner=uuid.uuid4(),platform_message_id='1',
                    member_message_ids=[1],version_key='a'*64,sender_id=sender,sender_nickname='隔离身份',
                    source_version_at=when,posted_at=when,detected_at=datetime.now(timezone.utc),
                    raw_text='隔离测试',media_state='ready')
                with patch('app.routes.telegram.FRESH_PRIORITY_WINDOW_SECONDS',300),patch('app.routes.telegram.REALTIME_WINDOW_SECONDS',86400):
                    job=review_job_metadata(uuid.uuid4(),body)
                self.assertEqual(expected,job['initial_state'])
                if expected=='unresolved':
                    self.assertEqual('historical_replay',job['review_mode'])
                    self.assertEqual(300,job['trace_payload']['realtime_window_seconds'])


@unittest.skipUnless(api_fixture.TEST_URL,'Explicit isolated temporary PostgreSQL is required')
class ChannelIdentityApiTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = api_fixture.TelegramApiTests.asyncTearDown
    admin_client = api_fixture.TelegramApiTests.admin_client
    event = api_fixture.TelegramApiTests.event
    post = api_fixture.TelegramApiTests.post

    async def asyncSetUp(self):
        await api_fixture.TelegramApiTests.asyncSetUp(self)
        self.app.include_router(admin.router)
        await self.admin_client()

    async def trust_channel(self):
        await self.pool.execute('UPDATE v2_source_checkpoints SET last_message_id=60 WHERE source_id=$1',self.source_id)
        response = await self.client.post(f'/api/v1/admin/sources/{self.source_id}/experts',
            json={'sender_id':'-1001','nickname':'明确选择的频道身份','verification_note':'隔离样本'})
        self.assertEqual(201,response.status_code,response.text)
        self.client.headers['Authorization'] = 'Bearer test-collector-token'
        return uuid.UUID(response.json()['id'])

    async def test_trusted_channel_is_returned_as_durable_backfill_and_can_advance(self):
        expert_id = await self.trust_channel()
        response = await self.client.get('/internal/telegram/sources')
        self.assertEqual(200,response.status_code,response.text)
        task = response.json()['items'][0]['author_backfills'][0]
        self.assertEqual('-1001',task['sender_id'])
        self.assertGreater(datetime.fromisoformat(task['since']),datetime.now(timezone.utc)-timedelta(hours=73))
        update = await self.client.put('/internal/telegram/author-backfill',json={
            'chat_id':'-1001','owner':str(self.owner),'sender_id':'-1001','previous_cursor':61,'cursor':40,'complete':False})
        self.assertEqual(200,update.status_code,update.text)
        self.assertEqual(60,await self.pool.fetchval('SELECT last_message_id FROM v2_source_checkpoints WHERE source_id=$1',self.source_id))
        self.assertEqual(40,await self.pool.fetchval('SELECT backfill_cursor FROM v2_source_experts WHERE source_id=$1 AND expert_id=$2',self.source_id,expert_id))

    async def test_channel_event_retry_keeps_one_message_and_one_review_job(self):
        await self.trust_channel()
        body = self.event()|{'sender_id':'-1001','sender_nickname':'频道发布身份'}
        for _ in range(2):
            response = await self.post(body)
            self.assertEqual(200,response.status_code,response.text)
        for table in ('v2_telegram_events','v2_expert_messages','v2_analysis_jobs'):
            self.assertEqual(1,await self.pool.fetchval(f'SELECT count(*) FROM {table}'))

    async def test_channel_historical_recovery_is_preserved_without_realtime_queue(self):
        await self.trust_channel()
        old=datetime.now(timezone.utc)-timedelta(hours=2)
        body=self.event()|{'sender_id':'-1001','posted_at':old.isoformat(),
            'source_version_at':old.isoformat(),'detected_at':datetime.now(timezone.utc).isoformat()}
        for _ in range(2):
            response=await self.post(body)
            self.assertEqual(200,response.status_code,response.text)
        self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_expert_messages'))
        row=await self.pool.fetchrow('SELECT state,review_mode,last_error FROM v2_analysis_jobs')
        self.assertEqual(('unresolved','historical_replay','historical_backlog_auto_suppressed_by_operator'),tuple(row))
        self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_analysis_traces'))

    async def test_channel_directory_metadata_never_grants_trust(self):
        self.client.headers['Authorization'] = 'Bearer test-collector-token'
        body={'chat_id':'-1001','owner':str(self.owner),'observed_at':datetime.now(timezone.utc).isoformat(),
            'state':'ready','admins':[{'sender_id':'-1001','nickname':'频道身份','admin_kind':'channel'}]}
        response=await self.client.put('/internal/telegram/directory',json=body)
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual(0,await self.pool.fetchval("SELECT count(*) FROM v2_experts WHERE platform_sender_id='-1001'"))
        body['observed_at']=datetime.now(timezone.utc).isoformat()
        body['admins'][0]['sender_id']='-1002'
        rejected=await self.client.put('/internal/telegram/directory',json=body)
        self.assertEqual(422,rejected.status_code)

    async def test_revoked_channel_mapping_cannot_replay_or_advance(self):
        expert_id = await self.trust_channel()
        self.client.headers.pop('Authorization',None)
        response = await self.client.delete(f'/api/v1/admin/sources/{self.source_id}/experts/{expert_id}')
        self.assertEqual(200,response.status_code,response.text)
        self.client.headers['Authorization'] = 'Bearer test-collector-token'
        self.assertEqual([], (await self.client.get('/internal/telegram/sources')).json()['items'][0]['author_backfills'])
        denied=await self.client.put('/internal/telegram/author-backfill',json={
            'chat_id':'-1001','owner':str(self.owner),'sender_id':'-1001','previous_cursor':61,'cursor':60,'complete':False})
        self.assertEqual(409,denied.status_code,denied.text)
        body=self.event()|{'sender_id':'-1001'}
        self.assertEqual(403,(await self.post(body)).status_code)

    async def test_backfill_rejects_invalid_identity_and_cross_source_advance(self):
        await self.trust_channel()
        for sender,status in [('0',422),('-0',422),('9223372036854775808',422),
                              ('-9223372036854775809',422),('-1002',409)]:
            with self.subTest(sender=sender):
                response=await self.client.put('/internal/telegram/author-backfill',json={
                    'chat_id':'-1001','owner':str(self.owner),'sender_id':sender,
                    'previous_cursor':61,'cursor':60,'complete':False})
                self.assertEqual(status,response.status_code,response.text)
