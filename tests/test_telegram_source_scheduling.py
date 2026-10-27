"""Bounded Telegram source work without turning timeouts into deletion facts."""
import asyncio
import unittest
from unittest.mock import patch

from telethon import utils
from telethon.errors import FloodWaitError

from collector.telegram import TelegramCollector
from test_telegram_collector import Backend, Client, CONFIG, Message


class ScheduledBackend(Backend):
    def __init__(self):
        super().__init__()
        self.scheduled = {}
        self.observation_calls = []
        self.claims = []

    async def checkpoint(self, chat_id, owner):
        self.claims.append(chat_id)
        return await super().checkpoint(chat_id,owner)

    async def observed(self, chat_id, owner, limit=50):
        self.observation_calls.append((chat_id,limit))
        rows=self.scheduled.setdefault(chat_id,[])
        batch=rows[:limit]
        del rows[:limit]
        return batch


class SourceSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend=ScheduledBackend()
        self.client=Client()
        self.collector=TelegramCollector(CONFIG,backend=self.backend,client_factory=lambda *_:self.client)
        self.source=self.backend.source_items[0]
        self.collector.client=self.client
        self.collector.sources={'-1001':self.source}

    async def asyncTearDown(self):
        await self.collector.disconnect()

    def queue(self,chat,groups):
        self.backend.scheduled[chat]=[{'content':{'member_message_ids':group}} for group in groups]

    async def test_batch_fetch_deduplicates_album_ids_and_renews_before_each_commit(self):
        self.client.messages['-1001']=[Message(i) for i in range(1,14)]
        self.queue('-1001',[[1,2],[2,3],*[ [i] for i in range(4,12)]])
        calls=[]
        original=self.client.get_messages
        async def fetch(entity,ids):
            calls.append(list(ids));return await original(entity,ids)
        self.client.get_messages=fetch
        report=await self.collector._reconcile_known('-1001',self.source)
        self.assertEqual({'state':'ready','checked':10,'pending':0},report)
        self.assertEqual([list(range(1,12))],calls)
        self.assertEqual([('-1001',10)],self.backend.observation_calls)
        self.assertEqual(11,len(self.backend.claims))
        self.assertFalse(self.collector.locks['-1001'].locked())

    async def test_slow_rpc_is_deferred_and_original_claim_retries_before_new_observation(self):
        self.client.messages['-1001']=[Message(1)]
        self.queue('-1001',[[1]])
        original=self.client.get_messages
        async def blocked(*_,**__): await asyncio.Future()
        self.client.get_messages=blocked
        with patch('collector.telegram.RECONCILE_RPC_SECONDS',.02):
            report=await asyncio.wait_for(self.collector._reconcile_known('-1001',self.source),.4)
        self.assertEqual('deferred',report['state'])
        self.assertEqual(1,report['pending'])
        self.assertEqual([],self.backend.calls)
        self.assertFalse(self.collector.locks['-1001'].locked())
        self.client.get_messages=original
        recovered=await self.collector._reconcile_known('-1001',self.source)
        self.assertEqual(1,recovered['checked'])
        self.assertEqual(1,len(self.backend.observation_calls))
        self.assertEqual(1,len(self.backend.events))

    async def test_cancelled_reconciliation_keeps_unchecked_claim_and_reenters_once(self):
        self.client.messages['-1001']=[Message(1)]
        self.queue('-1001',[[1]])
        entered=asyncio.Event()
        original=self.client.get_messages
        async def blocked(*_,**__):
            entered.set();await asyncio.Future()
        self.client.get_messages=blocked
        task=asyncio.create_task(self.collector._reconcile_known('-1001',self.source))
        await asyncio.wait_for(entered.wait(),.5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(1,len(self.collector.reconcile_pending['-1001']))
        self.assertFalse(self.collector.locks['-1001'].locked())
        self.client.get_messages=original
        await self.collector._reconcile_known('-1001',self.source)
        self.assertEqual(1,len(self.backend.events))
        self.assertEqual(1,len(self.backend.observation_calls))

    async def test_failed_later_id_batch_never_marks_earlier_missing_ids_deleted(self):
        self.queue('-1001',[list(range(1,51)),list(range(51,101)),[101]])
        calls=[]
        async def partially_blocked(entity,ids):
            calls.append(list(ids))
            if 101 in ids:await asyncio.Future()
            return []
        self.client.get_messages=partially_blocked
        with patch('collector.telegram.RECONCILE_RPC_SECONDS',.02):
            report=await self.collector._reconcile_known('-1001',self.source)
        self.assertEqual('deferred',report['state'])
        self.assertEqual([100,1],[len(ids) for ids in calls])
        self.assertEqual([],self.backend.calls)
        self.assertEqual(3,len(self.collector.reconcile_pending['-1001']))

    async def test_four_sources_progress_despite_three_slow_reconciliation_rpcs(self):
        chats=['-1001','-1002','-1003','-1004']
        self.backend.source_items=[self.source|{'chat_id':chat} for chat in chats]
        self.client.messages={chat:[Message(i+1)] for i,chat in enumerate(chats)}
        for i,chat in enumerate(chats):self.queue(chat,[[i+1]])
        original=self.client.get_messages
        async def fetch(entity,ids):
            if str(utils.get_peer_id(entity))!='-1004':await asyncio.Future()
            return await original(entity,ids)
        self.client.get_messages=fetch
        with patch('collector.telegram.RECONCILE_RPC_SECONDS',.02):
            result=await asyncio.wait_for(self.collector.sync_once(),.8)
        self.assertEqual([1,2,3,4],[self.backend.positions[chat] for chat in chats])
        self.assertEqual('caught_up',result['sources']['-1004']['state'])
        self.assertEqual(['reconciliation_deferred']*3,[result['sources'][chat]['state'] for chat in chats[:3]])
        self.assertEqual(4,len(self.backend.events))
        self.assertTrue(all(not lock.locked() for lock in self.collector.locks.values()))
        self.assertEqual(set(chats),set(self.backend.claims[-4:]))

    async def test_history_deadline_releases_slot_and_restart_replays_persisted_event_idempotently(self):
        self.client.messages['-1001']=[Message(1)]
        original=self.backend.event
        persisted=asyncio.Event()
        async def saved_then_stalled(body):
            await original(body);persisted.set();await asyncio.Future()
        self.backend.event=saved_then_stalled
        with patch('collector.telegram.SOURCE_HISTORY_SECONDS',.02):
            report=await asyncio.wait_for(self.collector.sync_once(),.5)
        self.assertTrue(persisted.is_set())
        self.assertEqual('connected',report['state'])
        self.assertEqual('unavailable',report['sources']['-1001']['state'])
        self.assertEqual('history_deadline',report['sources']['-1001']['reason'])
        self.assertEqual(0,self.backend.positions.get('-1001',0))
        self.assertEqual(1,len(self.backend.events))
        self.assertFalse(self.collector.locks['-1001'].locked())
        self.backend.event=original
        restarted=TelegramCollector(CONFIG,backend=self.backend,client_factory=lambda *_:self.client)
        try:
            result=await asyncio.wait_for(restarted.sync_once(),.5)
            self.assertEqual('caught_up',result['sources']['-1001']['state'])
            self.assertEqual(1,self.backend.positions['-1001'])
            self.assertEqual(1,len(self.backend.events))
            self.assertEqual(2,len(self.backend.calls))
        finally:await restarted.disconnect()

    async def test_lease_refresh_failure_is_visible_without_stopping_other_sources(self):
        self.client.messages['-1001']=[Message(1)]
        self.client.messages['-1002']=[Message(2)]
        self.backend.source_items.append(self.source|{'chat_id':'-1002'})
        original=self.backend.checkpoint
        async def claim(chat,owner):
            if chat=='-1001' and self.backend.claims.count(chat)>=2:raise ConnectionError('isolated lease unavailable')
            return await original(chat,owner)
        self.backend.checkpoint=claim
        result=await self.collector.sync_once()
        self.assertEqual('lease_deferred',result['sources']['-1001']['state'])
        self.assertEqual('deferred',result['sources']['-1001']['lease_state'])
        self.assertEqual(1,self.backend.positions['-1001'])
        self.assertEqual('caught_up',result['sources']['-1002']['state'])
        self.assertEqual(2,self.backend.positions['-1002'])

    async def test_persistence_failure_does_not_pop_claim_and_retry_keeps_full_original(self):
        self.client.messages['-1001']=[Message(1,text='原始完整推荐')]
        self.queue('-1001',[[1]])
        self.backend.fail_chat='-1001'
        with self.assertRaises(ConnectionError):
            await self.collector._reconcile_known('-1001',self.source)
        self.assertEqual(1,len(self.collector.reconcile_pending['-1001']))
        self.assertEqual([],self.backend.calls)
        self.backend.fail_chat=None
        await self.collector._reconcile_known('-1001',self.source)
        self.assertEqual('原始完整推荐',self.backend.calls[0]['raw_text'])
        self.assertEqual(1,len(self.backend.observation_calls))

    async def test_observed_claim_limit_does_not_truncate_remaining_remote_rotation(self):
        self.client.messages['-1001']=[Message(i) for i in range(1,24)]
        self.queue('-1001',[[i] for i in range(1,24)])
        sizes=[]
        for _ in range(3):
            report=await self.collector._reconcile_known('-1001',self.source)
            sizes.append(report['checked'])
        self.assertEqual([10,10,3],sizes)
        self.assertEqual(23,len(self.backend.events))
        self.assertEqual([],self.backend.scheduled['-1001'])

    async def test_flood_wait_retains_claim_then_success_after_backoff(self):
        self.client.messages['-1001']=[Message(1)]
        self.queue('-1001',[[1]])
        original=self.client.get_messages
        async def flood(*_,**__):raise FloodWaitError(None,capture=30)
        self.client.get_messages=flood
        first=await self.collector.sync_once()
        self.assertEqual('flood_wait',first['sources']['-1001']['state'])
        self.assertEqual(1,len(self.collector.reconcile_pending['-1001']))
        second=await self.collector.sync_once()
        self.assertEqual('backoff',second['sources']['-1001']['state'])
        self.client.get_messages=original
        self.collector.retry_at['-1001']=0
        third=await self.collector.sync_once()
        self.assertEqual('caught_up',third['sources']['-1001']['state'])
        self.assertEqual(1,len(self.backend.observation_calls))
        self.assertEqual(1,len(self.backend.events))


if __name__=='__main__':unittest.main()
