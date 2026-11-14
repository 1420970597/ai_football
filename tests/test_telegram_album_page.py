"""Batch album neighbourhoods without changing completeness or retry semantics."""
from unittest.mock import AsyncMock, Mock
import unittest
import test_telegram_recent_recovery as fixtures


class AlbumPageTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.RecentRecoveryTests.asyncSetUp
    asyncTearDown=fixtures.RecentRecoveryTests.asyncTearDown
    recent=fixtures.RecentRecoveryTests.recent
    async def test_page_albums_share_neighbourhood_read_without_merging_groups(self):
        self.client.messages['-1001']=[self.recent(i,grouped=g,media=True)
            for g,ids in [(22,[100,101]),(23,[105,106]),(24,[110,111])]for i in ids]
        self.collector._schedule_live_media=Mock()
        self.client.get_messages=AsyncMock(side_effect=self.client.get_messages)
        result=await self.collector._recover_recent('-1001',self.source)
        self.assertEqual('caught_up',result['state'])
        self.assertEqual(1,self.client.get_messages.await_count)
        self.assertEqual(3,result['persisted'])
        self.assertEqual({(100,101),(105,106),(110,111)},
            {tuple(body['member_message_ids'])for body in self.backend.calls})
        self.assertEqual(3,self.collector._schedule_live_media.call_count)

    async def test_distant_neighbourhoods_are_chunked_with_missing_heads_preserved(self):
        heads={i:self.recent(i,grouped=i,media=True)for i in range(100,1000,100)}
        self.client.get_messages=AsyncMock(return_value=[])
        groups=await self.collector._expand_page_albums(object(),heads)
        self.assertTrue(all(len(call.kwargs['ids'])<=100 for call in self.client.get_messages.await_args_list))
        requested={i for call in self.client.get_messages.await_args_list for i in call.kwargs['ids']}
        expected={j for i in heads for j in range(i-20,i+21)}
        self.assertEqual(expected,requested)
        self.assertEqual({k:[v]for k,v in heads.items()},groups)

    async def test_partial_rpc_failure_never_marks_any_album_as_committed(self):
        self.client.messages['-1001']=[self.recent(i,grouped=i,media=True)for i in [100,200,300]]
        original=self.client.get_messages
        self.client.get_messages=AsyncMock(side_effect=[[],ConnectionError('temporary')])
        with self.assertLogs('platform.telegram',level='WARNING'):
            failed=await self.collector._recover_recent('-1001',self.source)
        self.assertEqual('deferred',failed['state'])
        self.assertFalse(self.backend.calls)
        self.assertFalse(self.collector.recent_recovery['-1001']['receipts'])
        self.client.get_messages=AsyncMock(side_effect=original)
        self.assertEqual(3,(await self.collector._recover_recent('-1001',self.source))['persisted'])
