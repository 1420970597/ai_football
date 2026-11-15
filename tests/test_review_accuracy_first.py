"""Accuracy-first reviews may exceed 20s; eligibility still needs evidence."""
import os
import unittest
import uuid
from datetime import datetime,timedelta,timezone
from unittest.mock import AsyncMock,patch

from fastapi import HTTPException
from app import review_deadline,review_state,worker
from app.review_recovery import market_recovery
import test_reviews as support


class AccuracyFirstPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_detection_age_never_creates_a_default_signal_cutoff(self):
        self.assertIsNone(review_deadline.REALTIME_SIGNAL_DEADLINE_SECONDS)
        now=datetime.now(timezone.utc)
        for age in (0,20,60,600,86400):
            stamp=now-timedelta(seconds=age)
            self.assertIsNone(review_deadline.review_deadline_at(stamp))
            self.assertIsNone(review_deadline.remaining_review_seconds(stamp,now=now))

    async def test_worker_continues_a_queued_review_older_than_twenty_seconds(self):
        job={'id':uuid.uuid4(),'attempt_id':uuid.uuid4(),'review_mode':'real_time',
             'message':{'detected_at':datetime.now(timezone.utc)-timedelta(seconds=90)}}
        process=AsyncMock();expire=AsyncMock()
        with patch.object(worker,'_process_job',process),patch.object(worker,'_expire_detection_deadline',expire):
            await worker.process_job(object(),object(),job)
        process.assert_awaited_once();expire.assert_not_awaited()
        self.assertGreaterEqual(worker.REVIEW_TIMEOUT,60)

    async def test_transient_market_recovery_is_not_suppressed_by_detection_age(self):
        now=datetime.now(timezone.utc);conn=AsyncMock()
        conn.fetch.return_value=[{'fixture_id':'fixture','reason':'refresh_timeout','retry_after':None}]
        job={'id':uuid.uuid4(),'message_id':uuid.uuid4(),'review_mode':'real_time',
             'posted_at':now,'detected_at':now-timedelta(seconds=90)}
        result=await market_recovery(conn,job,uuid.uuid4(),1)
        self.assertEqual('temporary_market_refresh_failure',result['reason'])
        self.assertEqual(20,result['delay_seconds'])
        conn.fetch.assert_awaited_once()


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated PostgreSQL')
class AccuracyFirstEvidenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewTests.asyncSetUp
    asyncTearDown=support.ReviewTests.asyncTearDown
    context=support.ReviewTests.context
    post=support.ReviewTests.post
    fixture=support.ReviewTests.fixture
    search_all=support.ReviewTests.search_all
    resolve=support.ReviewTests.resolve
    read_market=support.ReviewTests.read_market

    async def prepare_old_detection(self):
        await self.pool.execute("UPDATE v2_expert_messages SET detected_at=now()-interval '90 seconds' WHERE id=$1",self.message)
        fixture=await self.fixture();key,_=await self.search_all();await self.resolve(key)
        quote=await self.read_market(fixture)
        return fixture,quote

    async def test_search_binding_and_current_quote_recording_work_after_twenty_seconds(self):
        fixture,quote=await self.prepare_old_detection()
        response=await self.post('record_expert_signals',signals=[{'fixture_id':fixture,
            'quote_id':quote['quote_id'],'evidence_text':'Alpha1 Beta1 full-time over 2.5'}])
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual(1,len(response.json()['accepted']))
        self.assertEqual(1,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_closed_quote_still_rejected_after_detection_cutoff_removed(self):
        fixture,quote=await self.prepare_old_detection()
        await self.pool.execute("UPDATE v2_current_quotes SET status='suspended'")
        response=await self.post('record_expert_signals',signals=[{'fixture_id':fixture,
            'quote_id':quote['quote_id'],'evidence_text':'Alpha1 Beta1 full-time over 2.5'}])
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual([],response.json()['accepted'])
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))

    async def test_expired_lease_and_revised_source_remain_ineligible(self):
        await self.prepare_old_detection()
        await self.pool.execute("UPDATE v2_analysis_jobs SET lease_until=now()-interval '1 second' WHERE id=$1",self.job)
        response=await self.post('search_matches',team_query='Other')
        self.assertEqual(409,response.status_code)
        self.assertIn('no longer active',response.text)
        await self.pool.execute("UPDATE v2_analysis_jobs SET lease_until=now()+interval '1 minute' WHERE id=$1",self.job)
        await self.pool.execute("UPDATE v2_expert_messages SET deleted_at=now() WHERE id=$1",self.message)
        response=await self.post('search_matches',team_query='Other')
        self.assertEqual(409,response.status_code)
        self.assertEqual(0,await self.pool.fetchval('SELECT count(*) FROM v2_expert_signals'))
