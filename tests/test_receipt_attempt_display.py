"""Old immutable receipts are retained while their current-attempt status is explicit."""
from datetime import datetime, timezone
import unittest
import uuid

from app import message_read
import test_admin_reviews as support


@unittest.skipUnless(support.ADMIN_URL, 'requires explicit isolated PostgreSQL')
class ReceiptAttemptTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.AdminReviewTests.asyncSetUp
    asyncTearDown = support.AdminReviewTests.asyncTearDown

    async def receipt(self, job, attempt, sequence=20):
        payload={'attempt_id':str(attempt),'accepted_count':7,'content':'系统已接受7条推荐'}
        await self.pool.execute(
            'INSERT INTO v2_analysis_traces(id,job_id,sequence_no,trace_kind,payload) VALUES($1,$2,$3,\'system_review_result\',$4)',
            uuid.uuid4(),job,sequence,payload)
        return payload

    async def test_same_job_retry_retains_old_payload_and_new_receipt_becomes_current(self):
        first,second=uuid.uuid4(),uuid.uuid4()
        await self.pool.execute("UPDATE v2_analysis_jobs SET attempt_id=$2,state='succeeded' WHERE id=$1",self.job,first)
        original=await self.receipt(self.job,first)
        row=await message_read.latest_summary(self.pool,self.message)
        self.assertTrue(row['is_current_attempt'])
        self.assertEqual(original,row['payload'])
        await self.pool.execute("UPDATE v2_analysis_jobs SET attempt_id=$2,state='retry',last_error='TimeoutError' WHERE id=$1",self.job,second)
        row=await message_read.latest_summary(self.pool,self.message)
        self.assertFalse(row['is_current_attempt'])
        self.assertEqual(second,row['current_attempt_id'])
        self.assertEqual('retry',row['state'])
        self.assertEqual(original,row['payload'])
        await self.receipt(self.job,second,21)
        row=await message_read.latest_summary(self.pool,self.message)
        self.assertTrue(row['is_current_attempt'])
        old=await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE job_id=$1 AND sequence_no=20",self.job)
        self.assertEqual(original,old)

    async def test_new_job_without_receipt_exposes_current_state_with_old_receipt(self):
        first=uuid.uuid4()
        await self.pool.execute("UPDATE v2_analysis_jobs SET attempt_id=$2,state='succeeded' WHERE id=$1",self.job,first)
        original=await self.receipt(self.job,first)
        newer=uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_analysis_jobs(id,message_id,state,idempotency_key,created_at) VALUES($1,$2,'queued',$3,now()+interval '1 second')",newer,self.message,str(newer))
        row=await message_read.latest_summary(self.pool,self.message)
        self.assertFalse(row['is_current_attempt'])
        self.assertEqual('queued',row['state'])
        self.assertIsNone(row['current_attempt_id'])
        self.assertEqual(self.job,row['review_id'])
        self.assertEqual(original,row['payload'])
