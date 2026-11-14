"""Session cleanup cannot retain a review slot for the old eight-second tail."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from app import worker


class ReviewSessionCleanupTests(unittest.IsolatedAsyncioTestCase):
    def job(self):
        message = uuid.uuid4()
        return {'id': uuid.uuid4(), 'attempt_id': uuid.uuid4(), 'attempts': 1,
                'message': {'id': message, 'raw_text': 'Synthetic explicit intent', 'media_objects': []}}

    async def test_cleanup_keeps_attempt_identity_and_shared_client(self):
        settings, client, job = object(), object(), self.job()
        for model_client in (None, client):
            cleanup = AsyncMock()
            with self.subTest(shared_client=model_client is not None), \
                 patch.object(worker, 'delete_session', cleanup):
                await worker._cleanup_session(settings, job, model_client=model_client)
                if model_client is None:
                    cleanup.assert_awaited_once_with(settings, job['id'], job['attempt_id'])
                else:
                    cleanup.assert_awaited_once_with(settings, job['id'], job['attempt_id'], client=client)

    async def test_stalled_cleanup_is_cancelled_and_drained_in_its_small_budget(self):
        stopped = asyncio.Event()

        async def stalled(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        with patch.object(worker, 'SESSION_CLEANUP_TIMEOUT_SECONDS', .02), \
             patch.object(worker, 'delete_session', side_effect=stalled), \
             self.assertLogs(worker.log, level='WARNING') as captured:
            await asyncio.wait_for(worker._cleanup_session(object(), self.job()), .5)
        self.assertTrue(stopped.is_set(), 'cleanup must be drained before the slot is released')
        self.assertIn('category=TimeoutError', '\n'.join(captured.output))

    async def test_cleanup_failure_does_not_expose_exception_content_or_fail_completed_job(self):
        with patch.object(worker, 'delete_session', AsyncMock(side_effect=RuntimeError('private body omitted'))), \
             self.assertLogs(worker.log, level='WARNING') as captured:
            await worker._cleanup_session(object(), self.job())
        output = '\n'.join(captured.output)
        self.assertIn('category=RuntimeError', output)
        self.assertNotIn('private body omitted', output)

    async def test_shutdown_cancellation_is_propagated_after_cleanup_io_stops(self):
        started, stopped = asyncio.Event(), asyncio.Event()

        async def stalled(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        with patch.object(worker, 'delete_session', side_effect=stalled):
            task = asyncio.create_task(worker._cleanup_session(object(), self.job()))
            await asyncio.wait_for(started.wait(), .5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, .5)
        self.assertTrue(stopped.is_set())

    async def test_real_process_finally_uses_bounded_cleanup_after_committed_conclusion(self):
        stopped = asyncio.Event()
        complete = AsyncMock()

        async def stalled(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        async def heartbeat(*args, **kwargs):
            await asyncio.Event().wait()

        settings = SimpleNamespace(hermes_model='isolated')
        with patch.object(worker, 'SESSION_CLEANUP_TIMEOUT_SECONDS', .02), \
             patch.object(worker, 'resolve_review_settings', AsyncMock(return_value=settings)), \
             patch.object(worker, '_add_trace', AsyncMock()), \
             patch.object(worker, '_review_until_verified', AsyncMock(return_value=('none', {'content': 'none'}))), \
             patch.object(worker, 'keep_lease', side_effect=heartbeat), \
             patch.object(worker, '_complete_review', complete), \
             patch.object(worker, 'delete_session', side_effect=stalled), \
             self.assertLogs(worker.log, level='WARNING'):
            await asyncio.wait_for(worker._process_job(object(), settings, self.job()), .5)
        complete.assert_awaited_once()
        self.assertTrue(stopped.is_set())

    async def test_four_stalled_cleanups_release_capacity_for_the_next_expert(self):
        entered, fifth_started = asyncio.Event(), asyncio.Event()
        stopped = []
        jobs = [self.job() for _ in range(5)]
        queued = iter(jobs)
        client = SimpleNamespace(aclose=AsyncMock())
        cleaning = 0

        async def cleanup(settings, review_id, attempt_id, **kwargs):
            nonlocal cleaning
            cleaning += 1
            if cleaning == 4:
                entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(review_id)

        async def claim(pool):
            return next(queued, None)

        async def process(pool, settings, job, *, model_client):
            if job is jobs[4]:
                fifth_started.set()
                return
            await worker._cleanup_session(settings, job, model_client=model_client)

        async def listen(url, wake, stop):
            await stop.wait()

        with patch.object(worker, 'CONCURRENCY', 4), \
             patch.object(worker, 'SESSION_CLEANUP_TIMEOUT_SECONDS', .02), \
             patch.object(worker, 'load_settings', return_value=SimpleNamespace(database_url='isolated')), \
             patch.object(worker.database, 'open_pool', AsyncMock(return_value=object())), \
             patch.object(worker.database, 'close_pool', AsyncMock()), \
             patch.object(worker.httpx, 'AsyncClient', return_value=client), \
             patch.object(worker, 'listen_for_jobs', side_effect=listen), \
             patch.object(worker, 'cancel_ineligible_reviews', AsyncMock()), \
             patch.object(worker, 'claim_job', side_effect=claim), \
             patch.object(worker, 'process_job', side_effect=process), \
             patch.object(worker, 'delete_session', side_effect=cleanup), \
             self.assertLogs(worker.log, level='WARNING'):
            runtime = asyncio.create_task(worker.run())
            try:
                await asyncio.wait_for(entered.wait(), .5)
                self.assertFalse(fifth_started.is_set())
                await asyncio.wait_for(fifth_started.wait(), 1)
            finally:
                runtime.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(runtime, .5)
        self.assertCountEqual([job['id'] for job in jobs[:4]], stopped)
        client.aclose.assert_awaited_once()
