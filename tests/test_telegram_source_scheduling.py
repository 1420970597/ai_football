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
        self.ack_calls = []

    async def checkpoint(self, chat_id, owner):
        self.claims.append(chat_id)
        return await super().checkpoint(chat_id,owner)

    async def observed(self, chat_id, owner, limit=50):
        self.observation_calls.append((chat_id,limit))
        rows=self.scheduled.setdefault(chat_id,[])
        batch=rows[:limit]
        del rows[:limit]
        return [self.claimed_observation(item) for item in batch]

    async def acknowledge_observed(self, chat_id, owner, reconciliation):
        self.ack_calls.append((chat_id,reconciliation))
        return await super().acknowledge_observed(chat_id,owner,reconciliation)


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

    async def test_batch_fetch_deduplicates_album_ids_without_redundant_checkpoint(self):
        self.client.messages['-1001']=[Message(i) for i in range(1,14)]
        self.queue('-1001',[[1,2],[2,3],*[ [i] for i in range(4,12)]])
        calls=[]
        original=self.client.get_messages
        async def fetch(entity,ids):
            calls.append(list(ids));return await original(entity,ids)
        self.client.get_messages=fetch
        with patch('collector.telegram.RECONCILE_EVENT_LIMIT',10):
            report=await self.collector._reconcile_known('-1001',self.source)
        self.assertEqual({'state':'ready','checked':10,'pending':0},report)
        self.assertEqual([list(range(1,12))],calls)
        self.assertEqual([('-1001',10)],self.backend.observation_calls)
        self.assertEqual([],self.backend.claims)
        self.assertEqual(10,len(self.backend.ack_calls))
        self.assertNotIn('-1001',self.collector.locks)

    async def test_slow_rpc_is_deferred_and_original_claim_retries_before_new_observation(self):
        self.client.messages['-1001']=[Message(1)]
        self.queue('-1001',[[1]])
        original=self.client.get_messages
        async def blocked(*_,**__): await asyncio.Future()
        self.client.get_messages=blocked
        with patch('collector.telegram.RECONCILE_RPC_SECONDS',.02):
            with self.assertLogs('platform.telegram',level='WARNING') as logs:
                report=await asyncio.wait_for(self.collector._reconcile_known('-1001',self.source),.4)
        self.assertEqual('deferred',report['state'])
        self.assertEqual(1,report['pending'])
        self.assertIn('stage=telegram_messages',logs.output[0])
        self.assertEqual([],self.backend.calls)
        self.assertNotIn('-1001',self.collector.locks)
        self.client.get_messages=original
        recovered=await self.collector._reconcile_known('-1001',self.source)
        self.assertEqual(1,recovered['checked'])
        self.assertEqual(1,len(self.backend.observation_calls))
        self.assertEqual(1,len(self.backend.events))

    async def test_reconciliation_message_read_can_exceed_three_seconds(self):
        self.client.messages['-1001']=[Message(1)]
        self.queue('-1001',[[1]])
        original=self.client.get_messages
        async def delayed(entity,ids):
            await asyncio.sleep(3.2)
            return await original(entity,ids)
        self.client.get_messages=delayed
        report=await asyncio.wait_for(self.collector._reconcile_known('-1001',self.source),6)
        self.assertEqual({'state':'ready','checked':1,'pending':0},report)
        self.assertEqual(1,len(self.backend.events))
        self.assertEqual(1,len(self.backend.ack_calls))

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
        self.assertNotIn('-1001',self.collector.locks)
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
        self.assertTrue(all(self.backend.claims.count(chat)==2 for chat in chats))

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
            if chat=='-1001' and self.backend.claims.count(chat)>=1:raise ConnectionError('isolated lease unavailable')
            return await original(chat,owner)
        self.backend.checkpoint=claim
        result=await self.collector.sync_once()
        self.assertEqual('lease_deferred',result['sources']['-1001']['state'])
        self.assertEqual('deferred',result['sources']['-1001']['lease_state'])
        self.assertEqual(1,self.backend.positions['-1001'])
        self.assertEqual('caught_up',result['sources']['-1002']['state'])
        self.assertEqual(2,self.backend.positions['-1002'])

    async def test_completed_source_refreshes_before_next_source_wave(self):
        chats=['-1001','-1002','-1003','-1004']
        self.backend.source_items=[self.source|{'chat_id':chat,'trusted_sender_ids':[]} for chat in chats]
        slow_source_entered=asyncio.Event()
        release_slow_source=asyncio.Event()

        async def sync_source(chat_id, source):
            await self.collector._checkpoint(chat_id)
            if chat_id=='-1004':
                slow_source_entered.set()
                await release_slow_source.wait()
            return {'state':'caught_up','last_message_id':0,'observed':0}

        self.collector._sync_source=sync_source
        task=asyncio.create_task(self.collector.sync_once())
        try:
            await asyncio.wait_for(slow_source_entered.wait(),.5)
            self.assertTrue(any(self.backend.claims.count(chat)>=2 for chat in chats[:3]))
        finally:
            release_slow_source.set()
        result=await asyncio.wait_for(task,.8)
        self.assertTrue(all(self.backend.claims.count(chat)==2 for chat in chats))
        self.assertEqual(set(chats),set(result['sources']))

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
        for _ in range(8):
            report=await self.collector._reconcile_known('-1001',self.source)
            sizes.append(report['checked'])
        self.assertEqual([3,3,3,3,3,3,3,2],sizes)
        self.assertEqual(23,len(self.backend.events))
        self.assertEqual([],self.backend.scheduled['-1001'])

    async def test_slow_event_commits_finish_before_reconciliation_deadline(self):
        self.client.messages['-1001']=[Message(i) for i in range(1,4)]
        self.queue('-1001',[[1],[2],[3]])
        original=self.backend.event
        async def slow_event(body):
            await asyncio.sleep(4)
            return await original(body)
        self.backend.event=slow_event
        report=await asyncio.wait_for(self.collector._reconcile_known('-1001',self.source),16)
        self.assertEqual({'state':'ready','checked':3,'pending':0},report)
        self.assertEqual(3,len(self.backend.events))
        self.assertEqual(3,len(self.backend.calls))

    async def test_independent_commits_overlap_with_two_request_limit(self):
        self.client.messages['-1001']=[Message(i) for i in range(1,4)]
        self.queue('-1001',[[1],[2],[3]])
        entered=asyncio.Event()
        release=asyncio.Event()
        original=self.backend.event
        active=peak=0
        started=[]
        async def blocked(body):
            nonlocal active,peak
            active+=1
            peak=max(peak,active)
            started.append(body['platform_message_id'])
            if len(started)==2:entered.set()
            try:
                await release.wait()
                return await original(body)
            finally:active-=1
        self.backend.event=blocked
        task=asyncio.create_task(self.collector._reconcile_known('-1001',self.source))
        try:
            await asyncio.wait_for(entered.wait(),1)
            self.assertEqual(2,len(started))
            release.set()
            report=await asyncio.wait_for(task,1)
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        self.assertEqual({'state':'ready','checked':3,'pending':0},report)
        self.assertEqual(2,peak)
        self.assertEqual(0,active)
        self.assertEqual(3,len(self.backend.ack_calls))

    async def test_four_sources_share_three_reconciliation_commit_slots(self):
        chats=['-1001','-1002','-1003','-1004']
        sources=[self.source|{'chat_id':chat} for chat in chats]
        self.backend.source_items=sources
        self.client.messages={chat:[Message(i) for i in range(1,4)] for chat in chats}
        for chat in chats:self.queue(chat,[[1],[2],[3]])
        original=self.backend.event
        active=peak=0
        async def delayed_event(body):
            nonlocal active,peak
            active+=1
            peak=max(peak,active)
            try:
                await asyncio.sleep(.02)
                return await original(body)
            finally:active-=1
        self.backend.event=delayed_event
        reports=await asyncio.wait_for(
            asyncio.gather(*(self.collector._reconcile_known(chat,source)
                             for chat,source in zip(chats,sources))),2)
        self.assertEqual([{'state':'ready','checked':3,'pending':0}]*4,reports)
        self.assertEqual(3,peak)
        self.assertEqual(12,len(self.backend.events))
        self.assertEqual(12,len(self.backend.ack_calls))

    async def test_live_message_persists_while_reconciliation_slots_are_full(self):
        chats=['-1001','-1002','-1003']
        sources=[self.source|{'chat_id':chat} for chat in chats]
        self.backend.source_items=sources+[self.source|{'chat_id':'-1004'}]
        self.collector.sources={source['chat_id']:source for source in self.backend.source_items}
        self.client.messages={chat:[Message(1)] for chat in chats}
        for chat in chats:self.queue(chat,[[1]])
        original=self.backend.event
        recon_active=0
        recon_started=asyncio.Event()
        release=asyncio.Event()
        async def delayed_event(body):
            nonlocal recon_active
            if body['platform_message_id']=='99':
                return await original(body)
            recon_active+=1
            if recon_active==3:recon_started.set()
            try:
                await release.wait()
                return await original(body)
            finally:recon_active-=1
        self.backend.event=delayed_event
        # Start each source as a task so all three historical writes occupy the shared slots.
        task=asyncio.create_task(self._reconcile_sources(chats,sources))
        try:
            await asyncio.wait_for(recon_started.wait(),1)
            await asyncio.wait_for(self.collector._live_messages('-1004',[Message(99)]),.5)
            self.assertTrue(any(row['chat_id']=='-1004' and row['platform_message_id']=='99'
                                for row in self.backend.calls))
        finally:
            release.set()
        reports=await asyncio.wait_for(task,1)
        self.assertTrue(all(report['state']=='ready' for report in reports))
        self.assertEqual(0,recon_active)

    async def _reconcile_sources(self,chats,sources):
        return await asyncio.gather(*(self.collector._reconcile_known(chat,source)
                                      for chat,source in zip(chats,sources)))

    async def test_failed_neighbor_does_not_replay_acknowledged_item(self):
        self.client.messages['-1001']=[Message(i) for i in range(1,4)]
        self.queue('-1001',[[1],[2],[3]])
        acknowledged=asyncio.Event()
        event=self.backend.event
        ack=self.backend.acknowledge_observed
        async def acknowledge(*args):
            result=await ack(*args)
            acknowledged.set()
            return result
        async def fail_second(body):
            if body['platform_message_id']=='2':
                await acknowledged.wait()
                raise ConnectionError('isolated transient failure')
            return await event(body)
        self.backend.event=fail_second
        self.backend.acknowledge_observed=acknowledge
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(self.collector._reconcile_known('-1001',self.source),1)
        self.assertEqual([[2],[3]],[r['content']['member_message_ids'] for r in self.collector.reconcile_pending['-1001']])
        self.backend.event=event
        recovered=await self.collector._reconcile_known('-1001',self.source)
        self.assertEqual(2,recovered['checked'])
        self.assertEqual(3,len(self.backend.calls))
        self.assertEqual(3,len(self.backend.ack_calls))
        self.assertEqual(1,len(self.backend.observation_calls))

    async def test_cancelled_parallel_commit_keeps_unknown_payload_and_awaits_tasks(self):
        self.client.messages['-1001']=[Message(i) for i in range(1,4)]
        self.queue('-1001',[[1],[2],[3]])
        acknowledged=asyncio.Event()
        stopped=asyncio.Event()
        event=self.backend.event
        ack=self.backend.acknowledge_observed
        async def acknowledge(*args):
            result=await ack(*args)
            acknowledged.set()
            return result
        async def unknown_first(body):
            result=await event(body)
            if body['platform_message_id']=='1':
                try:await asyncio.Future()
                finally:stopped.set()
            return result
        self.backend.event=unknown_first
        self.backend.acknowledge_observed=acknowledge
        task=asyncio.create_task(self.collector._reconcile_known('-1001',self.source))
        await asyncio.wait_for(acknowledged.wait(),1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(stopped.is_set())
        pending=self.collector.reconcile_pending['-1001']
        self.assertEqual([[1],[3]],[r['content']['member_message_ids'] for r in pending])
        self.assertEqual('1',pending[0]['_reconcile_submission']['payload']['platform_message_id'])
        self.backend.event=event
        recovered=await self.collector._reconcile_known('-1001',self.source)
        self.assertEqual(2,recovered['checked'])
        self.assertEqual(3,len(self.backend.events))
        self.assertEqual(3,len(self.backend.ack_calls))
        self.assertEqual(1,len(self.backend.observation_calls))

    async def test_three_slow_commits_fit_budget_without_checkpoint_per_event(self):
        self.client.messages['-1001']=[Message(i) for i in range(1,4)]
        self.queue('-1001',[[1],[2],[3]])
        checkpoint=self.backend.checkpoint
        event=self.backend.event
        async def slow_checkpoint(*args):
            await asyncio.sleep(.15)
            return await checkpoint(*args)
        async def slow_event(body):
            await asyncio.sleep(.15)
            return await event(body)
        self.backend.checkpoint=slow_checkpoint
        self.backend.event=slow_event
        with patch('collector.telegram.RECONCILE_BATCH_SECONDS',.8):
            report=await asyncio.wait_for(self.collector._reconcile_known('-1001',self.source),1.5)
        self.assertEqual({'state':'ready','checked':3,'pending':0},report)
        self.assertEqual([],self.backend.claims)
        self.assertEqual(3,len(self.backend.events))
        self.assertEqual(3,len(self.backend.ack_calls))

    async def test_atomic_event_response_skips_compatibility_ack_call(self):
        self.client.messages['-1001']=[Message(1)]
        self.queue('-1001',[[1]])
        original=self.backend.event
        async def atomic_event(body):
            return (await original(body)) | {'reconciliation_acknowledged':True}
        self.backend.event=atomic_event
        report=await self.collector._reconcile_known('-1001',self.source)
        self.assertEqual({'state':'ready','checked':1,'pending':0},report)
        self.assertEqual(1,len(self.backend.events))
        self.assertEqual([],self.backend.ack_calls)

    async def test_live_message_can_persist_while_reconciliation_claim_waits(self):
        entered,release=asyncio.Event(),asyncio.Event()
        async def blocked_claim(*_args,**_kwargs):
            entered.set()
            await release.wait()
            return []
        self.backend.observed=blocked_claim
        task=asyncio.create_task(self.collector._reconcile_known('-1001',self.source))
        await asyncio.wait_for(entered.wait(),.5)
        await asyncio.wait_for(self.collector._live_messages('-1001',[Message(99)]),.5)
        self.assertIn(('-1001','99'),[(item['chat_id'],item['platform_message_id']) for item in self.backend.calls])
        release.set()
        await asyncio.wait_for(task,.5)

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

    async def test_long_flood_backoff_renews_database_lease_for_live_events(self):
        database_clock=[0.0]
        lease_until=[0.0]
        claim=self.backend.checkpoint
        event=self.backend.event
        async def renew(chat,owner):
            result=await claim(chat,owner)
            lease_until[0]=database_clock[0]+90
            return result
        async def leased_event(body):
            if database_clock[0]>=lease_until[0]:
                raise RuntimeError('lease_inactive')
            return await event(body)
        self.backend.checkpoint=renew
        self.backend.event=leased_event
        self.client.messages['-1001']=[Message(1)]
        self.queue('-1001',[[1]])
        reads=[]
        async def flood(*_,**__):
            reads.append(True)
            raise FloodWaitError(None,capture=180)
        self.client.get_messages=flood
        first=await self.collector.sync_once()
        self.assertEqual('flood_wait',first['sources']['-1001']['state'])
        calls=len(self.backend.claims)
        database_clock[0]=120
        second=await self.collector.sync_once()
        self.assertEqual('backoff',second['sources']['-1001']['state'])
        self.assertEqual(calls+1,len(self.backend.claims))
        self.assertEqual([True],reads,'official history RPC must remain paused')
        await self.collector._live_messages('-1001',[Message(99,text='退避期间实时原文')])
        self.assertTrue(any(row['platform_message_id']=='99' for row in self.backend.calls))
        self.assertEqual(1,self.backend.positions['-1001'],'lease renewal must not advance history')
        self.assertEqual(1,len(self.collector.reconcile_pending['-1001']))

    async def test_backoff_lease_failure_is_reported_without_telegram_retry(self):
        import time
        self.collector.retry_at['-1001']=time.monotonic()+180
        async def unavailable(*_,**__):
            raise ConnectionError('isolated database unavailable')
        self.backend.checkpoint=unavailable
        result=await self.collector.sync_once()
        row=result['sources']['-1001']
        self.assertEqual('lease_deferred',row['state'])
        self.assertEqual('backoff',row['history_state'])
        self.assertEqual('deferred',row['lease_state'])
        self.assertEqual('ConnectionError',row['lease_error'])
        self.assertEqual([],self.client.query_limits)
        self.assertEqual([],self.backend.calls)


if __name__=='__main__':unittest.main()
