"""Real cancellation and lock boundaries; no live providers or private payloads."""
import asyncio
import re
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx

from services.collector.leyu_protocol import LeyuError
from services.collector.leyu_refresh import LeyuRefreshController, LeyuRequestBudget
from services.collector.worker import CollectorService
from services.collector.quote_snapshot import DeliveredFixtureSnapshots
from services.collector.upload_gate import UploadGate


def fixture():
    return {'id': 'leyu:telemetry-private-fixture', 'source_id': 'leyu:telemetry-private-fixture',
            'source_match_id': 'telemetry-private-fixture', 'phase': 'live', 'markets': [],
            'markets_complete': True, 'competition_name': 'telemetry-private-payload'}


class RefreshTelemetryTests(unittest.IsolatedAsyncioTestCase):
    def controller(self, fetch=None, upload=None):
        row = fixture()
        runtime = SimpleNamespace(fixtures={row['id']: row}, request_budget=LeyuRequestBudget(),
                                  refresh_match=fetch or AsyncMock(return_value=row.copy()))
        return LeyuRefreshController(runtime, upload or AsyncMock(return_value=1), deadline_seconds=.03)

    async def test_source_deadline_records_phase_and_never_uploads(self):
        upload = AsyncMock(return_value=1)
        cancelled = asyncio.Event()

        async def fetch(mid):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        controller = self.controller(fetch, upload)
        with self.assertLogs('platform.collector.refresh', level='INFO') as logs:
            result = await controller.refresh({'fixture_id': fixture()['id'], 'source_match_id': fixture()['source_match_id']})
        self.assertEqual('refresh_timeout', result['reason'])
        self.assertTrue(cancelled.is_set())
        upload.assert_not_called()
        self.assertIn('stage=source', logs.output[0])
        self.assertIn('ingestion_ms=0.0', logs.output[0])
        self.assertNotIn('telemetry-private', logs.output[0])

    async def test_ingestion_deadline_does_not_acknowledge_unknown_commit(self):
        entered = asyncio.Event()

        async def upload(rows):
            entered.set()
            await asyncio.Event().wait()

        controller = self.controller(upload=upload)
        with self.assertLogs('platform.collector.refresh', level='INFO') as logs:
            result = await controller.refresh({'fixture_id': fixture()['id'], 'source_match_id': fixture()['source_match_id']})
        self.assertTrue(entered.is_set())
        self.assertFalse(result['accepted'])
        self.assertNotIn('snapshot_committed', result)
        self.assertEqual(0, controller.cooldown_remaining())
        self.assertEqual({}, controller._inflight)
        self.assertIn('outcome=refresh_timeout stage=ingestion', logs.output[0])
        self.assertNotIn('telemetry-private', logs.output[0])

    async def test_last_waiter_cancel_and_sensitive_error_are_not_exposed(self):
        entered = asyncio.Event()

        async def upload(rows):
            entered.set()
            await asyncio.Event().wait()

        controller = self.controller(upload=upload)
        controller.deadline_seconds = 1
        with self.assertLogs('platform.collector.refresh', level='INFO') as logs:
            task = asyncio.create_task(controller.refresh({'fixture_id': fixture()['id'], 'source_match_id': fixture()['source_match_id']}))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual({}, controller._inflight)
        self.assertIn('outcome=cancelled stage=ingestion', logs.output[0])
        controller = self.controller(fetch=AsyncMock(side_effect=LeyuError('private-secret=url', 'telemetry-private-password')))
        with self.assertLogs('platform.collector.refresh', level='INFO') as logs:
            result = await controller.refresh({'fixture_id': fixture()['id'], 'source_match_id': fixture()['source_match_id']})
        self.assertEqual('refresh_failed', result['reason'])
        self.assertNotIn('private', ' '.join(logs.output))


class UploadTelemetryTests(unittest.IsolatedAsyncioTestCase):
    def service(self, post):
        service = CollectorService.__new__(CollectorService)
        service.api = SimpleNamespace(post=post)
        service.api_url = 'http://telemetry-private-host/internal'
        service._upload_lock = UploadGate(max_active=2)
        service._delivered_snapshots = DeliveredFixtureSnapshots()
        return service

    async def test_waiting_lock_cancel_sends_nothing_and_keeps_lock_owner(self):
        post = AsyncMock()
        service = self.service(post)
        await service._upload_lock.acquire()
        with self.assertLogs('platform.collector', level='INFO') as logs:
            task = asyncio.create_task(service.upload([fixture()]))
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(service._upload_lock.locked())
        service._upload_lock.release()
        post.assert_not_called()
        self.assertIn('outcome=cancelled stage=waiting_lock', logs.output[0])
        self.assertIn('http_ms=0.0', logs.output[0])
        self.assertNotIn('telemetry-private', logs.output[0])

    async def test_dispatched_request_cancel_releases_lock_without_retry_or_receipt(self):
        entered = asyncio.Event()

        async def post(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        mocked = AsyncMock(side_effect=post)
        service = self.service(mocked)
        with self.assertLogs('platform.collector', level='INFO') as logs:
            task = asyncio.create_task(service.upload([fixture()]))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(service._upload_lock.locked())
        self.assertEqual(1, mocked.await_count)
        self.assertFalse(hasattr(service, 'last_upload_report'))
        self.assertIn('outcome=cancelled stage=request', logs.output[0])
        self.assertIn('response_status=unknown', logs.output[0])
        self.assertNotIn('telemetry-private', logs.output[0])

    async def test_receipt_success_and_transport_error_remain_distinct(self):
        response = httpx.Response(200, json={'accepted': 1}, request=httpx.Request('POST', 'http://private.invalid'))
        service = self.service(AsyncMock(return_value=response))
        with self.assertLogs('platform.collector', level='INFO') as logs:
            self.assertEqual(1, await service.upload([fixture()]))
        self.assertEqual(1, service.last_upload_report['batches'])
        self.assertIn('outcome=response stage=request', logs.output[0])
        self.assertIn('response_status=200 fixture_count=1', logs.output[0])
        self.assertEqual(3, len(re.findall(r'(?:lock_wait_ms|http_ms|elapsed_ms)=[0-9]+\.[0-9]', logs.output[0])))
        service = self.service(AsyncMock(side_effect=httpx.ConnectError('telemetry-private-password')))
        with self.assertLogs('platform.collector', level='INFO') as logs:
            with self.assertRaises(httpx.ConnectError):
                await service.upload([fixture()])
        self.assertIn('outcome=transport_error', logs.output[0])
        self.assertNotIn('telemetry-private', logs.output[0])
