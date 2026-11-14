import asyncio
import unittest
from unittest.mock import AsyncMock,patch
from test_telegram_recent_recovery import RecentRecoveryTests


class ArchiveFairnessTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=RecentRecoveryTests.asyncSetUp
    asyncTearDown=RecentRecoveryTests.asyncTearDown
    recent=RecentRecoveryTests.recent

    async def test_recent_below_half_deadline_still_reserves_archive_rpc_slice(self):
        self.client.messages['-1001']=[self.recent(4)]
        async def head(*args):
            await asyncio.sleep(.025)
            return {'state':'caught_up','head_id':4}
        self.collector._recover_recent=AsyncMock(side_effect=head)
        with patch('collector.telegram.SOURCE_HISTORY_SECONDS',.1),\
                patch('collector.telegram.ARCHIVE_PREP_SLICE_SECONDS',.02):
            result=await self.collector._sync_source('-1001',self.source)
        self.assertEqual('fair_slice_yield',result['archive_history']['reason'])
        self.assertEqual(0,self.backend.positions.get('-1001',0))
        await self.collector._sync_source('-1001',self.source)
        self.assertEqual(4,self.backend.positions['-1001'])

    async def test_cold_retention_lookup_yields_before_archive_page_when_budget_spent(self):
        self.client.messages['-1001']=[self.recent(4)]
        self.collector.source_runtime['-1001']={'archive_priority':True}
        async def floor(*args):
            await asyncio.sleep(.04)
            return 1
        self.collector._retained_history_start=AsyncMock(side_effect=floor)
        with patch('collector.telegram.SOURCE_HISTORY_SECONDS',.06):
            result=await self.collector._sync_source('-1001',self.source)
        self.assertEqual('retention_slice_yield',result['archive_history']['reason'])
        self.assertEqual(0,self.backend.positions.get('-1001',0))
        self.assertTrue(self.collector.source_runtime['-1001']['archive_priority'])
        self.collector._retained_history_start=AsyncMock(return_value=1)
        await self.collector._sync_source('-1001',self.source)
        self.assertEqual(4,self.backend.positions['-1001'])

    async def test_slow_head_yields_then_archive_progresses_without_repeating_head(self):
        self.client.messages['-1001']=[self.recent(i)for i in range(1,5)]
        async def head(*args):
            await asyncio.sleep(.04)
            return {'state':'caught_up','head_id':4}
        self.collector._recover_recent=AsyncMock(side_effect=head)
        with patch('collector.telegram.SOURCE_HISTORY_SECONDS',.06):
            first=await self.collector._sync_source('-1001',self.source)
        self.assertEqual('catching_up',first['state'])
        self.assertEqual('fair_slice_yield',first['archive_history']['reason'])
        self.assertEqual(0,self.backend.positions.get('-1001',0))
        second=await self.collector._sync_source('-1001',self.source)
        self.assertEqual(4,self.backend.positions['-1001'])
        self.assertEqual(1,self.collector._recover_recent.await_count)
        self.assertEqual('catching_up',second['state'])
        await self.collector._sync_source('-1001',self.source)
        self.assertEqual(2,self.collector._recover_recent.await_count)

    async def test_recent_partial_at_archive_head_is_catching_up_not_failed(self):
        self.backend.positions['-1001']=4
        self.collector._recover_recent=AsyncMock(return_value={'state':'catching_up','head_id':4})
        result=await self.collector._sync_source('-1001',self.source)
        self.assertEqual('catching_up',result['state'])
