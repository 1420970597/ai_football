"""Offline HTTP/WS races; source timestamps remain provider facts."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from services.collector.leyu_runtime import LeyuRuntime


class SnapshotMonotonicTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = httpx.AsyncClient()
        self.runtime = LeyuRuntime(self.api, 'http://isolated.invalid/internal')
        self.runtime.load = AsyncMock(return_value={})

    async def asyncTearDown(self):
        await self.api.aclose()

    def quote(self, time='2026-10-27T12:00:00+00:00', **updates):
        return {'market_id':'4','period':'full_time','line':'-0.5','selection':'home',
                'price':'2.00','status':'suspended','source_updated_at':time,
                'source_time_quality':'known' if time else 'unknown',
                'received_at':'2026-10-27T12:00:01+00:00',
                'market_instance_id':'instance','selection_id':'option', **updates}

    def fixture(self, quotes, *, complete=True):
        return {'id':'leyu:one','source_id':'leyu:one','source_match_id':'one','phase':'live',
                'markets':deepcopy(quotes),'markets_complete':complete}

    async def collect(self, quotes, *, complete=True, during=None):
        async def fetch(**kwargs):
            if during:
                during()
            return [self.fixture(quotes,complete=complete)]
        client=SimpleNamespace(fetch=fetch,last_report={})
        with patch('services.collector.leyu_runtime.LeyuClient.from_config',return_value=client):
            return (await self.runtime.collect())[0]

    def push(self, quote):
        return self.runtime.apply_push([{'fixture_id':'leyu:one','received_at':'2026-10-27T12:01:00+00:00',
                                         'markets':[quote]}])

    async def test_old_complete_http_new_ws_old_complete_http_keeps_new_status_price_and_time(self):
        old=self.quote();await self.collect([old])
        newer=self.quote('2026-10-27T12:00:10+00:00',price='2.20',status='active',status_evidence=True)
        self.push(newer)
        result=await self.collect([old])
        kept=result['markets'][0]
        self.assertTrue(result['markets_complete'])
        self.assertEqual(('2.20','active',newer['source_updated_at']),
                         (kept['price'],kept['status'],kept['source_updated_at']))
        pushed,_=self.push({**newer,'source_updated_at':'2026-10-27T12:00:12+00:00',
                            'price':'2.22','status':'suspended','status_evidence':False})
        self.assertEqual('active',pushed[0]['markets'][0]['status'])

    async def test_priority_read_during_station_poll_preserves_new_quote_and_fixture_context(self):
        old = self.quote(status='active')
        await self.collect([old])
        newer = self.quote('2026-10-27T12:00:10+00:00', line='-0.75',price='2.20',status='active')
        async def fetch(**kwargs):
            async def detail(mid, **kw):
                return {**self.fixture([newer]), 'phase':'live','home_score':1,
                        'received_at':'2026-10-27T12:00:11+00:00'}
            with patch('services.collector.leyu_runtime.LeyuClient.from_config',
                       return_value=SimpleNamespace(fetch_match=detail)):
                await self.runtime.refresh_match('one')
            return [{**self.fixture([old]),'phase':'scheduled','home_score':0,
                     'received_at':'2026-10-27T12:00:01+00:00'}]
        with patch('services.collector.leyu_runtime.LeyuClient.from_config',
                   return_value=SimpleNamespace(fetch=fetch,last_report={})):
            result = (await self.runtime.collect())[0]
        self.assertEqual(('live',1),(result['phase'],result['home_score']))
        self.assertFalse(result['markets_complete'])
        self.assertIn(newer,result['markets'])

    async def test_unknown_or_malformed_http_never_erases_known_quote_or_invents_source_time(self):
        known=self.quote(status='active');await self.collect([known])
        for time in (None,'invalid','2026-10-27T12:00:30'):
            with self.subTest(time=time):
                result=await self.collect([self.quote(time,price='3.00',received_at='2026-10-27T12:10:00+00:00')])
                self.assertEqual(known,result['markets'][0])
        self.runtime.fixtures={}
        result=await self.collect([self.quote(None)])
        self.assertIsNone(result['markets'][0]['source_updated_at'])
        self.assertEqual('unknown',result['markets'][0]['source_time_quality'])

    async def test_newer_complete_http_updates_even_when_price_unchanged_and_offset_is_normalized(self):
        await self.collect([self.quote('2026-10-27T13:00:00+01:00')])
        fresh=self.quote('2026-10-27T12:00:01Z',status='active')
        result=await self.collect([fresh])
        self.assertEqual(fresh,result['markets'][0])

    async def test_complete_absence_keeps_authoritative_removal_and_partial_absence_preserves(self):
        first=self.quote();other=self.quote(selection='away',selection_id='away')
        await self.collect([first,other])
        partial=await self.collect([first],complete=False)
        self.assertFalse(partial['markets_complete']);self.assertEqual(2,len(partial['markets']))
        complete=await self.collect([first])
        self.assertTrue(complete['markets_complete']);self.assertEqual([first],complete['markets'])
        empty=await self.collect([])
        self.assertTrue(empty['markets_complete']);self.assertEqual([],empty['markets'])

    async def test_concurrent_ws_new_option_missing_from_full_response_is_not_falsely_closed(self):
        first=self.quote();await self.collect([first])
        added=self.quote('2026-10-27T12:00:10+00:00',selection='away',selection_id='away',status='active',status_evidence=True)
        result=await self.collect([first],during=lambda:self.push(added))
        self.assertFalse(result['markets_complete'])
        self.assertEqual({'home','away'},{q['selection'] for q in result['markets']})
        # A subsequent complete request with no concurrent delta keeps real deletion semantics.
        next_result=await self.collect([first])
        self.assertTrue(next_result['markets_complete']);self.assertEqual([first],next_result['markets'])
        self.assertEqual({},self.runtime._market_push_revisions)

    async def test_concurrent_newer_ws_same_key_is_retained_without_downgrading_complete(self):
        first=self.quote();await self.collect([first])
        newer=self.quote('2026-10-27T12:00:10+00:00',price='2.20',status='active',status_evidence=True)
        result=await self.collect([first],during=lambda:self.push(newer))
        self.assertTrue(result['markets_complete']);self.assertEqual('2.20',result['markets'][0]['price'])

    async def test_unknown_time_ws_cannot_create_concurrent_preservation_evidence(self):
        first=self.quote();await self.collect([first])
        unknown=self.quote(None,selection='away',selection_id='away',status='active',status_evidence=True)
        def during():
            output,resync=self.push(unknown)
            self.assertEqual([],output);self.assertTrue(resync)
        result=await self.collect([],during=during)
        self.assertTrue(result['markets_complete']);self.assertEqual([],result['markets'])


if __name__=='__main__':
    unittest.main()
