"""Transient retry backoff must fit the original real-time detection budget."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app import worker


class ReviewRetryDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def run_failure(self, *, remaining=16, mode='real_time', attempts=1,
                          error=None, delay=None, trace_error=False):
        job = {'id': 'synthetic-review', 'attempt_id': 'synthetic-attempt',
               'attempts': attempts, 'review_mode': mode,
               'message': {'id': 'synthetic-message', 'raw_text': '',
                           'media_objects': [], 'detected_at': datetime.now(timezone.utc)}}
        pool = SimpleNamespace(execute=AsyncMock())
        settings = SimpleNamespace(hermes_model='isolated', review_config_url=None)
        trace = AsyncMock(side_effect=RuntimeError() if trace_error else None)
        cleanup = AsyncMock()

        async def heartbeat(*args, **kwargs):
            await asyncio.Event().wait()

        with patch.object(worker, 'remaining_review_seconds', return_value=remaining), \
             patch.object(worker, 'resolve_review_settings', AsyncMock(return_value=settings)), \
             patch.object(worker, '_add_trace', trace), \
             patch.object(worker, '_review_until_verified', AsyncMock(
                 side_effect=error or worker.HermesProviderUnavailable())), \
             patch.object(worker, 'keep_lease', side_effect=heartbeat), \
             patch.object(worker, '_cleanup_session', cleanup), \
             patch.object(worker.random, 'randint', return_value=0):
            if delay is None:
                await worker.process_job(pool, settings, job)
            else:
                with patch.object(worker, 'retry_delay_seconds', return_value=delay):
                    await worker.process_job(pool, settings, job)
        cleanup.assert_awaited_once()
        pool.execute.assert_awaited_once()
        self.assertIn("attempt_id=$2 AND state='running'", pool.execute.await_args.args[0])
        return pool.execute.await_args.args[3:], trace

    async def test_provider_backoff_beyond_budget_terminates_without_reclaim(self):
        (state, delay, reason), trace = await self.run_failure()
        self.assertEqual(('unresolved', 0, 'retry_exceeds_detection_deadline'), (state, delay, reason))
        budget = [c.args[3] for c in trace.await_args_list
                  if c.args[2] == 'review_retry_budget_exhausted']
        self.assertEqual(1, len(budget))
        self.assertEqual(60, budget[0]['retry_delay_seconds'])
        self.assertEqual(16, budget[0]['remaining_detection_seconds'])
        self.assertFalse(budget[0]['retryable'])
        self.assertTrue(budget[0]['accepted_evidence_preserved'])

    async def test_exact_deadline_is_too_late_to_start(self):
        result, _ = await self.run_failure(remaining=5, delay=5)
        self.assertEqual(('unresolved', 0, 'retry_exceeds_detection_deadline'), result)

    async def test_short_retry_keeps_backoff_when_it_fits(self):
        result, trace = await self.run_failure(error=RuntimeError())
        self.assertEqual(('retry', 5, 'RuntimeError'), result)
        self.assertFalse(any(c.args[2] == 'review_retry_budget_exhausted' for c in trace.await_args_list))

    async def test_remaining_budget_is_checked_again_after_failure_not_reset(self):
        result, _ = await self.run_failure(remaining=4, error=RuntimeError())
        self.assertEqual(('unresolved', 0, 'retry_exceeds_detection_deadline'), result)

    async def test_legacy_unknown_detection_keeps_existing_backoff(self):
        result, _ = await self.run_failure(remaining=None)
        self.assertEqual(('retry', 60, 'HermesProviderUnavailable'), result)

    async def test_historical_review_keeps_existing_backoff(self):
        result, _ = await self.run_failure(mode='historical_replay')
        self.assertEqual(('retry', 60, 'HermesProviderUnavailable'), result)

    async def test_permanent_failure_remains_failed(self):
        result, _ = await self.run_failure(error=RuntimeError('unsupported Telegram image'))
        self.assertEqual(('failed', 0, 'RuntimeError'), result)

    async def test_nonpersistent_attempt_limit_remains_failed(self):
        result, _ = await self.run_failure(attempts=worker.MAX_ATTEMPTS, error=RuntimeError())
        self.assertEqual(('failed', 0, 'RuntimeError'), result)

    async def test_persistent_retry_cannot_bypass_detection_budget(self):
        result, _ = await self.run_failure(attempts=worker.MAX_ATTEMPTS)
        self.assertEqual(('unresolved', 0, 'retry_exceeds_detection_deadline'), result)
