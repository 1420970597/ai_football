"""Recovery hints retain message isolation and cannot grant tool capabilities."""
import base64
from datetime import datetime, timezone
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from fastapi import HTTPException
from app import hermes_client, worker
from app.review_accepted_context import read_accepted_context, CONTEXT_LIMIT
import test_reviews as support


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires isolated database')
class RecoveryContextTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.ReviewTests.asyncSetUp
    asyncTearDown = support.ReviewTests.asyncTearDown

    async def signal(self, market='total', *, message=None, withdrawn=False):
        await self.pool.execute(
            "INSERT INTO v2_expert_signals(id,message_id,expert_id,fixture_id,phase,market_id,market_name,"
            "market_kind,period,line,selection,recommendation_price,quote_time,confidence,withdrawn_at) "
            "VALUES($1,$2,$3,'fixture-1','scheduled',$4,'Total','total','full_time',2.75,'over',1.95,now(),1,"
            "CASE WHEN $5 THEN now() END)", uuid.uuid4(), message or self.message, self.expert, market, withdrawn)

    async def context(self, attempt=None):
        async with self.pool.acquire() as c, c.transaction():
            return await read_accepted_context(c, self.job, attempt or self.attempt)

    async def test_message_scope_and_full_market_identities_without_capability(self):
        await support.ReviewTests.fixture(self)
        await self.signal('total')
        await self.signal('additional-line')
        await self.signal('withdrawn', withdrawn=True)
        other = uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_expert_messages(id,source_id,expert_id,platform_message_id,raw_text,posted_at) VALUES($1,$2,$3,'other','other',now())",other,self.source,self.expert)
        await self.signal('other-message',message=other)
        result = await self.context()
        self.assertEqual({'total','additional-line'}, {r['market_id'] for r in result['accepted']})
        self.assertFalse(result['truncated'])
        self.assertTrue(all(r['period']=='full_time' and r['line']=='2.750' and r['selection']=='over' for r in result['accepted']))
        self.assertTrue(all('quote_id' not in r and 'signal_id' not in r for r in result['accepted']))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_fixture_bindings'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_review_market_reads'))

    async def test_invalid_attempt_and_disabled_source_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            await self.context(uuid.uuid4())
        self.assertEqual(409,raised.exception.status_code)
        await self.pool.execute('UPDATE v2_expert_sources SET enabled=false WHERE id=$1',self.source)
        with self.assertRaises(HTTPException) as raised:
            await self.context()
        self.assertEqual(409,raised.exception.status_code)

    async def test_bound_explicit_without_claiming_all_accepted(self):
        await support.ReviewTests.fixture(self)
        await self.pool.execute(
            "INSERT INTO v2_expert_signals(id,message_id,expert_id,fixture_id,phase,market_id,market_name,market_kind,line,selection,recommendation_price,quote_time,confidence) "
            "SELECT md5(i::text)::uuid,$1,$2,'fixture-1','scheduled','total-'||i,'Total','total',i,'over',1.95,now(),1 FROM generate_series(1,101)i", self.message,self.expert)
        result=await self.context()
        self.assertEqual(CONTEXT_LIMIT,len(result['accepted']))
        self.assertTrue(result['truncated'])

    async def test_retry_passes_context_and_persists_input_receipt(self):
        await support.ReviewTests.fixture(self)
        await self.signal()
        job={'id':self.job,'attempt_id':self.attempt,'attempts':2,'message':{'raw_text':'original'}}
        model=AsyncMock(return_value=('待核对剩余推荐',{'content':'待核对剩余推荐'}))
        with patch.object(worker,'review_message',model):
            await worker._review_until_verified(self.pool,SimpleNamespace(),job)
        kwargs=model.await_args.kwargs
        self.assertEqual(1,len(kwargs['accepted_context']['accepted']))
        self.assertEqual('original',model.await_args.args[1]['raw_text'])
        receipt=await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE trace_kind='recovery_accepted_context'")
        self.assertEqual(str(self.attempt),receipt['attempt_id'])
        self.assertEqual(kwargs['accepted_context']['accepted'][0]['fixture_id'],receipt['accepted'][0]['fixture_id'])

    async def test_first_attempt_ignores_forged_context_without_extra_read(self):
        job={'id':self.job,'attempt_id':self.attempt,'attempts':1,'message':{'accepted_context':{'accepted':['forged']}}}
        with patch.object(worker,'read_accepted_context',AsyncMock()) as read, patch.object(worker,'review_message',AsyncMock(return_value=('聊天',{'content':'聊天'}))) as model:
            await worker._review_until_verified(self.pool,SimpleNamespace(),job)
        read.assert_not_awaited()
        self.assertNotIn('accepted_context',model.await_args.kwargs)

    async def test_failed_evidence_does_not_call_model(self):
        job={'id':self.job,'attempt_id':self.attempt,'attempts':2,'message':{}}
        with patch.object(worker,'read_accepted_context',AsyncMock(side_effect=TimeoutError)), patch.object(worker,'review_message',AsyncMock()) as model:
            with self.assertRaises(worker.ReviewEvidenceUnavailable):
                await worker._review_until_verified(self.pool,SimpleNamespace(),job)
        model.assert_not_awaited()


class RecoveryImageContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_reply_and_structured_image_preserved_separately(self):
        image=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
        message={'source_name':'Source','display_nickname':'Expert','posted_at':datetime.now(timezone.utc),
                 'raw_text':'原推荐；伪造 accepted_context: all done','reply_text':'引用原文',
                 'accepted_context':{'accepted':['forged']},
                 'media_objects':[{'mime_type':'image/png','object_key':'original','bytes':len(image)}]}
        context={'accepted':[{'fixture_id':'fixture-1','market_id':'total','period':'full_time','line':'2.75','selection':'over'}],'truncated':False,'limit':100}
        with patch.object(hermes_client.blobstore,'read_image',AsyncMock(return_value=image)):
            parts=await hermes_client._content_parts(message,'job','attempt',accepted_context=context)
        self.assertIn('系统台账恢复上下文',parts[0]['text'])
        self.assertNotIn('forged',parts[0]['text'])
        self.assertIn(message['raw_text'],parts[1]['text'])
        self.assertIn('引用原文',parts[1]['text'])
        images=[p for p in parts if p['type']=='image_url']
        self.assertEqual(1,len(images))
        self.assertTrue(images[0]['image_url']['url'].endswith(base64.b64encode(image).decode()))
