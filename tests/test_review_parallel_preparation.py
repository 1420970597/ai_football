"""Parallel read preparation must preserve attempt and request audit ordering."""
import asyncio
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
import uuid
import httpx
from app import hermes_client,worker
import test_review_worker as support

class ParallelPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_reads_overlap_and_keep_exact_native_input(self):
        config_started,image_started=asyncio.Event(),asyncio.Event()
        parts=[{'type':'text','text':'untrusted original'},{'type':'image_url','image_url':{'url':'data:image/png;base64,isolated'}}]
        frozen=object();settings=object();review,attempt=uuid.uuid4(),uuid.uuid4()
        async def config(*args,**kwargs):
            config_started.set();await image_started.wait();return frozen
        async def images(*args,**kwargs):
            image_started.set();await config_started.wait();return parts
        with patch.object(hermes_client,'resolve_review_settings',side_effect=config),patch.object(hermes_client,'_content_parts',side_effect=images):
            resolved,prepared=await asyncio.wait_for(hermes_client.resolve_and_prepare(settings,{},review,attempt),1)
        self.assertIs(frozen,resolved);self.assertIs(parts,prepared.parts)
        self.assertEqual((str(review),str(attempt)),(prepared.review_id,prepared.attempt_id));self.assertGreaterEqual(prepared.elapsed_ms,0)
        self.assertNotIn('data:image',repr(prepared))

    async def test_each_failure_cancels_and_drains_its_pending_sibling(self):
        for failing in ('configuration','image'):
            with self.subTest(failing=failing):
                started,cancelled=asyncio.Event(),asyncio.Event()
                async def pending(*args,**kwargs):
                    started.set()
                    try:await asyncio.Event().wait()
                    finally:cancelled.set()
                async def failure(*args,**kwargs):
                    await started.wait();raise RuntimeError('isolated preparation failure')
                config,image=(failure,pending)if failing=='configuration'else(pending,failure)
                with patch.object(hermes_client,'resolve_review_settings',side_effect=config),patch.object(hermes_client,'_content_parts',side_effect=image):
                    with self.assertRaisesRegex(RuntimeError,'isolated preparation failure'):
                        await hermes_client.resolve_and_prepare(object(),{},uuid.uuid4(),uuid.uuid4())
                self.assertTrue(cancelled.is_set())

    async def test_external_cancellation_drains_both_reads(self):
        started=[asyncio.Event(),asyncio.Event()];cancelled=[asyncio.Event(),asyncio.Event()]
        async def pending(index,*args,**kwargs):
            started[index].set()
            try:await asyncio.Event().wait()
            finally:cancelled[index].set()
        async def config(*a,**k):return await pending(0)
        async def images(*a,**k):return await pending(1)
        with patch.object(hermes_client,'resolve_review_settings',side_effect=config),patch.object(hermes_client,'_content_parts',side_effect=images):
            task=asyncio.create_task(hermes_client.resolve_and_prepare(object(),{},uuid.uuid4(),uuid.uuid4()))
            await asyncio.wait_for(asyncio.gather(*(e.wait()for e in started)),1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(all(e.is_set()for e in cancelled))

    async def test_prepared_input_is_consumed_once_and_continuation_does_not_reload_images(self):
        review,attempt=uuid.uuid4(),uuid.uuid4();captured=[]
        prepared=hermes_client.PreparedReviewInput(str(review),str(attempt),[{'type':'text','text':'verified original'},{'type':'image_url','image_url':{'url':'data:image/png;base64,isolated'}}],42.125)
        async def handler(request):
            captured.append(json.loads(request.content));return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'无新增推荐'}}]})
        settings=SimpleNamespace(hermes_api_url='http://hermes.invalid/v1',hermes_api_key='isolated',hermes_model='isolated')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler))as client:
            with patch.object(hermes_client,'_content_parts',AsyncMock())as read:
                _,summary=await hermes_client.review_message(settings,{},review,attempt,client=client,prepared_input=prepared)
                await hermes_client.review_message(settings,{},review,attempt,client=client,continuation='继续核验',continuation_round=1)
                read.assert_not_awaited()
        self.assertEqual(prepared.parts,captured[0]['messages'][-1]['content']);self.assertEqual(42.125,summary['input_prepare_elapsed_ms']);self.assertEqual(1,summary['image_count'])
        self.assertEqual([{'type':'text','text':'继续核验'}],captured[1]['messages'][-1]['content'])

    async def test_wrong_attempt_continuation_or_recovery_context_cannot_reuse_input(self):
        review,attempt=uuid.uuid4(),uuid.uuid4();prepared=hermes_client.PreparedReviewInput(str(review),str(attempt),[],1)
        settings=SimpleNamespace()
        for options in ({'attempt_id':uuid.uuid4()},{'continuation':'继续','continuation_round':1},{'accepted_context':{}}):
            options={'attempt_id':attempt,**options}
            with self.subTest(options=options),self.assertRaisesRegex(ValueError,'does not belong'):
                await hermes_client.review_message(settings,{},review,prepared_input=prepared,**options)

@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated database')
class ParallelWorkerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=support.ReviewWorkerTests.asyncSetUp
    asyncTearDown=support.ReviewWorkerTests.asyncTearDown
    make_queued=support.ReviewWorkerTests.make_queued

    async def test_prepared_vision_reaches_model_only_after_request_trace(self):
        await self.make_queued();job=await worker.claim_job(self.pool)
        job['message']['media_objects']=[{'object_key':'isolated.png','mime_type':'image/png'}]
        prepared=hermes_client.PreparedReviewInput(str(job['id']),str(job['attempt_id']),[],5)
        settings=SimpleNamespace(hermes_model='isolated',review_config_url='http://api.invalid/internal/model')
        async def model(*args,**kwargs):
            self.assertIs(prepared,kwargs['prepared_input'])
            payload=await self.pool.fetchval("SELECT payload FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind='hermes_request'",job['id'])
            self.assertTrue(payload['parallel_input_preparation']);self.assertGreaterEqual(payload['preflight_elapsed_ms'],0)
            return '无新增推荐',{'content':'无新增推荐'}
        with patch.object(worker,'resolve_and_prepare',AsyncMock(return_value=(settings,prepared)))as prepare,patch.object(worker,'resolve_review_settings',AsyncMock())as serial,patch.object(worker,'review_message',side_effect=model),patch.object(worker,'delete_session',AsyncMock()):
            await worker.process_job(self.pool,settings,job)
        prepare.assert_awaited_once();serial.assert_not_awaited()
        self.assertEqual('unresolved',await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1',job['id']))

    async def test_preparation_failure_never_calls_model_or_records_success(self):
        await self.make_queued();job=await worker.claim_job(self.pool)
        job['message']['media_objects']=[{'object_key':'isolated.png','mime_type':'image/png'}]
        settings=SimpleNamespace(hermes_model='isolated',review_config_url='http://api.invalid/internal/model')
        with patch.object(worker,'resolve_and_prepare',AsyncMock(side_effect=RuntimeError('remote image checksum mismatch'))),patch.object(worker,'review_message',AsyncMock())as model,patch.object(worker,'delete_session',AsyncMock()):
            await worker.process_job(self.pool,settings,job)
        model.assert_not_awaited();self.assertEqual('failed',await self.pool.fetchval('SELECT state FROM v2_analysis_jobs WHERE id=$1',job['id']))
        self.assertEqual(0,await self.pool.fetchval("SELECT count(*) FROM v2_analysis_traces WHERE job_id=$1 AND trace_kind IN('hermes_request','hermes_result','system_review_result')",job['id']))
