import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from collector.leyu import LeyuClient, DEFAULT_RESULT_DETAIL_PATH
from collector.leyu_protocol import LeyuError, RateLimited
from collector.leyu_results import normalize_result, fetch_result, ResultCollector


class NativeResultTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.target = {'fixture_id':'leyu:123','source_match_id':'123','lease_id':'isolated',
                       'kickoff_at':'2026-10-29T11:15:00Z','home_team':'Alpha','away_team':'Beta'}
        self.detail = {'mid':'123','csid':'1','mgt':1793272500000,'mhn':'Alpha','man':'Beta',
                       'ms':3,'mmp':'999','mft':0,'msc':['S1|2:0','S2|0:0','S3|2:0','S8|3:2']}

    def normalize(self, **changes):
        return normalize_result(self.detail | changes,self.target,requested_at=self.now,observed_at=self.now+timedelta(seconds=1))

    def test_verified_periods_and_unknown_source_clock(self):
        body = self.normalize()
        self.assertEqual((2,0,0,0),tuple(body[k] for k in ('home','away','half_home','half_away')))
        self.assertIsNone(body['source_updated_at'])
        self.assertEqual('unknown',body['evidence']['source_time_quality'])
        self.assertEqual(64,len(body['evidence']['decoded_response_sha256']))
        self.assertEqual(body,self.normalize())

    def test_wrong_match_identity_and_nonfinal_are_rejected(self):
        for changes, reason in (({'mid':'124'},'result_fixture_mismatch'),({'mgt':1793272501000},'result_identity_mismatch'),
                                ({'csid':'2'},'result_identity_mismatch'),({'mhn':'Wrong'},'result_identity_mismatch'),
                                ({'mgt':None},'result_identity_mismatch'),({'mmp':'6'},'result_not_final'),
                                ({'ms':909},'result_not_final'),({'mft':1},'result_period_unsupported')):
            with self.subTest(changes=changes),self.assertRaises(LeyuError) as raised:
                self.normalize(**changes)
            self.assertEqual(reason,raised.exception.category)

    def test_missing_duplicate_conflicting_and_extra_time_scores_are_rejected(self):
        for scores in (['S1|2:0'],['S1|2:0','S1|2:0','S2|0:0','S3|2:0'],
                       ['S1|2:0','S2|1:0','S3|2:0'],['S1|2:0','S2|0:0','S3|2:0','S7|1:0'],
                       ['S1|201:0','S2|0:0','S3|201:0'],['S1|?:0','S2|0:0','S3|2:0']):
            with self.subTest(scores=scores),self.assertRaises(LeyuError):
                self.normalize(msc=scores)

    async def test_native_get_uses_authorized_headers_mid_rdm_and_compressed_envelope(self):
        import gzip,base64
        seen=[]
        def handle(request):
            seen.append(request)
            data=base64.b64encode(gzip.compress(json.dumps(self.detail).encode())).decode()
            return httpx.Response(200,json={'code':'0000000','data':data})
        client=LeyuClient('https://native.invalid',request_id='isolated-native',transport=httpx.MockTransport(handle))
        body=await fetch_result(client,self.target)
        self.assertEqual(2,body['home'])
        self.assertEqual(1,len(seen))
        self.assertEqual(('GET',DEFAULT_RESULT_DETAIL_PATH),(seen[0].method,seen[0].url.path))
        self.assertEqual('123',seen[0].url.params['mid'])
        self.assertTrue(seen[0].url.params['rdm'].isdigit())
        self.assertEqual('isolated-native',seen[0].headers['requestid'])

    async def test_nonobject_detail_does_not_become_zero_score(self):
        client=LeyuClient('https://native.invalid',request_id='isolated',transport=httpx.MockTransport(
            lambda request:httpx.Response(200,json={'code':'0000000','data':[]})))
        with self.assertRaises(LeyuError) as raised:
            await fetch_result(client,self.target)
        self.assertEqual('invalid_result_detail',raised.exception.category)

    def runtime(self):
        return SimpleNamespace(version=1,config={},load=AsyncMock(),request_budget=SimpleNamespace(
            require_available=lambda:None,defer_requests=lambda seconds:None))

    async def test_hot_configuration_change_releases_lease_without_submitting_result(self):
        runtime=self.runtime()
        seen=[]
        async def changed(*args):
            runtime.version=2
            return self.normalize()
        def handle(request):
            seen.append(request)
            return httpx.Response(200,json={'ok':True})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as api:
            collector=ResultCollector(api,'http://api.invalid/internal',runtime,{})
            with patch('collector.leyu_results.fetch_result',side_effect=changed):
                outcome=await collector.check(self.target,None,1)
        self.assertEqual('result_configuration_changed',outcome['reason'])
        self.assertEqual(['/internal/collector/results/finish'],[r.url.path for r in seen])

    async def test_cancellation_never_submits_or_acknowledges(self):
        api=SimpleNamespace(post=AsyncMock())
        collector=ResultCollector(api,'http://api.invalid/internal',self.runtime(),{})
        with patch('collector.leyu_results.fetch_result',side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await collector.check(self.target,None,1)
        api.post.assert_not_awaited()

    async def test_lane_is_bounded_and_delivery_failure_visible(self):
        status={}
        api=SimpleNamespace(post=AsyncMock(return_value=httpx.Response(200,json={'items':[self.target]*4},request=httpx.Request('POST','http://api.invalid'))))
        collector=ResultCollector(api,'http://api.invalid/internal',self.runtime(),status)
        active=peak=0
        async def check(*args):
            nonlocal active,peak
            active+=1
            peak=max(peak,active)
            await asyncio.sleep(.001)
            active-=1
            raise httpx.ConnectError('private-source-detail')
        with patch.object(LeyuClient,'from_config',return_value=None),patch.object(collector,'check',side_effect=check):
            await collector.run_once()
        self.assertEqual(2,peak)
        self.assertEqual('partial',status['state'])
        self.assertEqual(4,status['checked'])
        self.assertNotIn('private-source-detail',str(status))
