"""Verified onboarding and hot refresh; only isolated DB and fake Telegram."""
import asyncio
from datetime import datetime, timezone, timedelta
import time
import unittest
import uuid

from telethon.errors import FloodWaitError

from collector.telegram import TelegramCollector
from collector.telegram_scheduling import TelegramReadBudget
from test_telegram_collector import Backend, Client, CONFIG, Message
import test_telegram_sources as source_support


@unittest.skipUnless(source_support.legacy.TEST_URL, '需要临时测试数据库')
class ChannelOnboardingApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = source_support.TelegramSourceTests.asyncSetUp
    asyncTearDown = source_support.TelegramSourceTests.asyncTearDown
    admin_client = source_support.TelegramSourceTests.admin_client
    fake_control = source_support.TelegramSourceTests.fake_control
    async def test_explicit_channel_onboarding_atomically_trusts_verified_publisher(self):
        calls = []
        with self.fake_control({'state':'ready','chat_id':'-1002','source_kind':'broadcast_channel'}, calls):
            response = await self.client.post('/api/v1/admin/sources', json={
                'chat_id':'-1002','display_name':'频道样本','monitor_channel_posts':True})
        self.assertEqual(201, response.status_code, response.text)
        self.assertTrue(response.json()['channel_publisher_trusted'])
        source_id = uuid.UUID(response.json()['id'])
        rows = await self.pool.fetch('SELECT e.platform_sender_id,se.enabled FROM v2_source_experts se '
            'JOIN v2_experts e ON e.id=se.expert_id WHERE se.source_id=$1', source_id)
        self.assertEqual([('-1002',True)], [(r['platform_sender_id'],r['enabled']) for r in rows])
        self.assertEqual('/source-info', calls[0]['path'])
        self.assertEqual(1, await self.pool.fetchval("SELECT count(*) FROM v2_audit_log WHERE action='telegram_channel_publisher_trusted'"))

    async def test_group_onboarding_does_not_authorize_everyone_or_own_peer(self):
        with self.fake_control({'state':'ready','chat_id':'-1002','source_kind':'group'}, []):
            response = await self.client.post('/api/v1/admin/sources', json={
                'chat_id':'-1002','display_name':'群组样本','monitor_channel_posts':True})
        self.assertEqual(201, response.status_code, response.text)
        self.assertFalse(response.json()['channel_publisher_trusted'])
        self.assertEqual('needs_author', response.json()['activation_state'])
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_source_experts WHERE source_id=$1', uuid.UUID(response.json()['id'])))

    async def test_unverified_or_wrong_peer_never_creates_false_monitored_source(self):
        for info in [{'state':'unavailable','reason':'account_has_not_joined'},
                     {'state':'ready','chat_id':'-1003','source_kind':'broadcast_channel'},
                     {'state':'ready','chat_id':'-1002','source_kind':'unknown'}]:
            with self.fake_control(info, []):
                response = await self.client.post('/api/v1/admin/sources', json={
                    'chat_id':'-1002','display_name':'不能核验','monitor_channel_posts':True})
            self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(0, await self.pool.fetchval("SELECT count(*) FROM v2_expert_sources WHERE platform_chat_id='-1002'"))

    async def test_no_opt_in_keeps_manual_author_contract(self):
        calls=[]
        with self.fake_control({'state':'ready'}, calls):
            response=await self.client.post('/api/v1/admin/sources',json={'chat_id':'-1002','display_name':'手工来源'})
        self.assertEqual(201,response.status_code)
        self.assertEqual([],calls)
        self.assertFalse(response.json()['channel_publisher_trusted'])

    async def test_explicit_reauthorization_is_required_after_revocation(self):
        endpoint=f'/api/v1/admin/sources/{self.source_id}/channel-publisher'
        with self.fake_control({'state':'ready','chat_id':'-1001','source_kind':'broadcast_channel'},[]):
            self.assertEqual(200,(await self.client.post(endpoint)).status_code)
        mapping=await self.pool.fetchrow("SELECT e.id FROM v2_experts e JOIN v2_source_experts se ON e.id=se.expert_id WHERE se.source_id=$1 AND e.platform_sender_id='-1001'", self.source_id)
        self.assertEqual(200,(await self.client.delete(f'/api/v1/admin/sources/{self.source_id}/experts/{mapping["id"]}')).status_code)
        self.assertFalse(await self.pool.fetchval('SELECT enabled FROM v2_source_experts WHERE source_id=$1 AND expert_id=$2',self.source_id,mapping['id']))
        with self.fake_control({'state':'ready','chat_id':'-1001','source_kind':'group'},[]):
            self.assertEqual(409,(await self.client.post(endpoint)).status_code)
        self.assertFalse(await self.pool.fetchval('SELECT enabled FROM v2_source_experts WHERE source_id=$1 AND expert_id=$2',self.source_id,mapping['id']))

    async def test_monitoring_requires_fresh_loaded_configuration_and_active_lease(self):
        now=datetime.now(timezone.utc)
        status={'connected':True,'authorized':True,'sources_loaded_at':now.isoformat(),
                'source_runtime':{'-1001':{'trusted_sender_count':1}},'source_health':{}}
        endpoint=f'/api/v1/admin/sources/{self.source_id}/monitoring'
        with self.fake_control(status,[]):
            response=await self.client.get(endpoint)
        self.assertEqual('monitoring',response.json()['state'],response.text)
        status['sources_loaded_at']=(now-timedelta(seconds=31)).isoformat()
        with self.fake_control(status,[]):
            self.assertEqual('waiting_collector',(await self.client.get(endpoint)).json()['state'])
        status['sources_loaded_at']=now.isoformat()
        await self.pool.execute("UPDATE v2_source_checkpoints SET lease_until=now()-interval '1 second' WHERE source_id=$1",self.source_id)
        with self.fake_control(status,[]):
            self.assertEqual('lease_unavailable',(await self.client.get(endpoint)).json()['state'])
        self.client.cookies.clear()
        with self.fake_control(status,[]):
            self.assertEqual(401,(await self.client.get(endpoint)).status_code)


class SourceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend=Backend();self.client=Client()
        self.collector=TelegramCollector(CONFIG,background_read_interval=0,backend=self.backend,client_factory=lambda *_:self.client)
        self.collector.client=self.client

    async def asyncTearDown(self):
        await self.collector.disconnect()

    async def test_hot_add_disable_and_trust_change_refresh_without_history_completion(self):
        await self.collector.refresh_sources(renew_leases=True)
        self.collector.recent_recovery['-1001']={'cursor':10}
        self.collector.recent_next_at['-1001']=time.monotonic()+60
        self.collector.archive_next_at['-1001']=time.monotonic()+300
        self.backend.source_items=[dict(self.backend.source_items[0],trusted_sender_ids=['8']),
            {'chat_id':'-1002','trusted_sender_ids':['7']}]
        await asyncio.wait_for(self.collector.refresh_sources(renew_leases=True),.2)
        self.assertEqual({'-1001','-1002'},set(self.collector.sources))
        self.assertNotIn('-1001',self.collector.recent_recovery)
        self.assertNotIn('-1001',self.collector.archive_next_at)
        self.assertIn('-1002',self.collector.source_runtime)
        self.backend.source_items=self.backend.source_items[1:]
        await self.collector.refresh_sources()
        self.assertNotIn('-1001',self.collector.sources)
        await self.collector._live_messages('-1001',[Message(1)])
        self.assertEqual(1,self.collector.live_event_delivery['unmapped_source'])
        self.assertEqual([],self.backend.calls)

    async def test_live_delivery_does_not_wait_for_background_budget_or_flood(self):
        await self.collector.refresh_sources()
        budget=self.collector.background_reads
        budget.next_at=budget.blocked_until=time.monotonic()+60
        await budget.lock.acquire()
        try:
            await asyncio.wait_for(self.collector._live_messages('-1001',[Message(1)]),.2)
        finally:
            budget.lock.release()
        self.assertEqual(1,len(self.backend.events))
        self.assertEqual(1,self.collector.source_runtime['-1001']['live_staged'])

    async def test_background_budget_serializes_sources_and_honors_global_flood_wait(self):
        budget=TelegramReadBudget(.015)
        stamps=[]
        async def read():
            async with budget.slot():stamps.append(time.monotonic())
        await asyncio.gather(read(),read(),read())
        self.assertTrue(all(b-a>=.013 for a,b in zip(stamps,stamps[1:])))
        with self.assertRaises(FloodWaitError):
            async with budget.slot():raise FloodWaitError(None,capture=10)
        self.assertGreater(budget.blocked_until,time.monotonic()+9)
        self.collector.background_reads=budget
        report=await self.collector._sync_source('-1001',self.backend.source_items[0])
        self.assertEqual('backoff',report['state'])
        self.assertEqual(1,budget.flood_waits)

    async def test_source_info_checks_real_type_not_negative_id(self):
        from telethon import utils
        from telethon.tl.types import Channel,ChatPhotoEmpty
        entity=Channel(id=2,title='广播样本',photo=ChatPhotoEmpty(),date=datetime.now(timezone.utc),broadcast=True)
        async def group(_):return entity
        self.collector._group_entity=group
        response=await self.collector.control('/source-info',{'chat_id':str(utils.get_peer_id(entity))})
        self.assertEqual('broadcast_channel',response['source_kind'])
        entity.broadcast=False;entity.megagroup=True
        self.assertEqual('group',(await self.collector.control('/source-info',{'chat_id':'-1002'}))['source_kind'])
