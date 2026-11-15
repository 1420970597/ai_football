"""A late explanation cannot hide independently committed recommendations."""
import unittest
import os
from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from app import worker
import test_reviews as support


class DeadlineReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_audit_only_receipt_preserves_count_without_claiming_completion(self):
        job={'id':'job','attempt_id':'attempt','message_id':'message','review_mode':'real_time'}
        evidence=SimpleNamespace(signals=[],total=2,rejected=[],identified=[])
        save=AsyncMock()
        with patch.object(worker,'read_summary_evidence',AsyncMock(return_value=evidence)), \
             patch.object(worker,'append_traces',save):
            await worker.record_deadline_summary(object(),job)
        kind,payload=save.call_args.args[2][0]
        self.assertEqual('system_review_result',kind)
        self.assertEqual(2,payload['accepted_count'])
        self.assertEqual('unresolved',payload['outcome'])
        self.assertFalse(payload['completion_verified'])
        self.assertTrue(payload['deadline_exceeded'])
        self.assertEqual('attempt',payload['attempt_id'])
        self.assertIn('禁止过期补录',payload['content'])


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated database')
class DeadlineReceiptIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    fixture=support.ReviewTests.fixture
    search_all=support.ReviewTests.search_all
    post=support.ReviewTests.post
    context=support.ReviewTests.context
    resolve=support.ReviewTests.resolve
    read_market=support.ReviewTests.read_market

    async def test_cutoff_keeps_real_committed_signal_and_receipt_is_idempotent(self):
        fixture=await self.fixture()
        key,_=await self.search_all()
        await self.resolve(key)
        quote=await self.read_market(fixture)
        result=await self.post('record_expert_signals',signals=[{'fixture_id':fixture,
            'quote_id':quote['quote_id'],'evidence_text':'Alpha1 Beta1 full-time over 2.5'}])
        self.assertEqual(1,len(result.json()['accepted']),result.text)
        before=dict(await self.pool.fetchrow('SELECT * FROM v2_expert_signals'))
        job={'id':self.job,'attempt_id':self.attempt,
            'review_mode':'real_time','message':{'id':self.message,'detected_at':datetime.now(timezone.utc)}}
        await worker._expire_detection_deadline(self.pool,job)
        await worker._expire_detection_deadline(self.pool,job)
        receipt=await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE trace_kind='system_review_result'")
        self.assertEqual(1,receipt['accepted_count'])
        self.assertEqual(str(before['id']),receipt['items'][0]['signal_id'])
        self.assertFalse(receipt['completion_verified'])
        self.assertEqual(before,dict(await self.pool.fetchrow('SELECT * FROM v2_expert_signals')))
        self.assertEqual(1,await self.pool.fetchval("SELECT count(*) FROM v2_analysis_traces WHERE trace_kind='system_review_result'"))
        self.assertEqual('unresolved',await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1',self.job))

    async def test_revoked_attempt_cannot_write_cutoff_receipt(self):
        await self.pool.execute("UPDATE v2_analysis_jobs SET state='failed',last_error='source_edited' WHERE id=$1",self.job)
        await worker._expire_detection_deadline(self.pool,{'id':self.job,'attempt_id':self.attempt})
        self.assertEqual(0,await self.pool.fetchval("SELECT count(*) FROM v2_analysis_traces WHERE trace_kind='system_review_result'"))

    async def test_audit_read_timeout_still_commits_deadline_fence_and_explicit_gap(self):
        job={'id':self.job,'attempt_id':self.attempt,'message':{'detected_at':datetime.now(timezone.utc)}}
        with patch.object(worker,'record_deadline_summary',AsyncMock(side_effect=TimeoutError)):
            await worker._expire_detection_deadline(self.pool,job)
        row=await self.pool.fetchrow('SELECT state,last_error,lease_until FROM v2_analysis_jobs WHERE id=$1',self.job)
        self.assertEqual(('unresolved','detection_deadline_exceeded',None),tuple(row))
        self.assertEqual(1,await self.pool.fetchval("SELECT count(*) FROM v2_analysis_traces WHERE trace_kind='deadline_summary_unavailable'"))

    async def test_no_signal_receipt_does_not_invent_success(self):
        evidence=SimpleNamespace(signals=[],total=0,rejected=[],identified=[])
        save=AsyncMock()
        with patch.object(worker,'read_summary_evidence',AsyncMock(return_value=evidence)), \
             patch.object(worker,'append_traces',save):
            await worker.record_deadline_summary(object(),{'id':'j','attempt_id':'a','message_id':'m'})
        payload=save.call_args.args[2][0][1]
        self.assertEqual(0,payload['accepted_count'])
        self.assertIn('不触发自动投注',payload['content'])
        self.assertFalse(payload['completion_verified'])
