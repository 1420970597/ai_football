import asyncio
import unittest
from unittest.mock import AsyncMock,patch
from test_telegram_recent_recovery import RecentRecoveryTests


class ArchiveFairnessTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=RecentRecoveryTests.asyncSetUp
    asyncTearDown=RecentRecoveryTests.asyncTearDown
    recent=RecentRecoveryTests.recent

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
