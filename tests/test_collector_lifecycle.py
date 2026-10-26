import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx

from services.collector.leyu_protocol import LeyuError
from services.collector.worker import CollectorService, fixture_batches, get_config, quote_freshness, wait_for_work


class FakeTelegram:
    def __init__(self, config, **kwargs):
        self.options = kwargs
        self.start_control_server = AsyncMock()
        self.apply_config = AsyncMock()
        self.sync_once = AsyncMock(return_value={"state": "disabled"})
        self.disconnect = AsyncMock()


class FakeRuntime:
    def __init__(self, *args):
        self.config = {}
        self.version = 1
        self.fixtures = {}
        self.last_report = {"complete_market_fixtures": 0}
        self.collect = AsyncMock(return_value=[])

    def apply_push(self, events):
        return [], True


class FakeRealtime:
    def __init__(self, *args):
        self.stats = {"state": "starting"}
        self.cancelled = False

    async def run(self, stop):
        try:
            await stop.wait()
        finally:
            self.cancelled = True


class CollectorLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.posts = []

        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"items": {"telegram_source_enabled": "false"}})
            self.posts.append(json.loads(request.content)["items"])
            return httpx.Response(200, json={"accepted": len(self.posts[-1])})

        self.api = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer test-only"})
        self.service = CollectorService(self.api, "http://api.invalid/internal", "test-only", interval=1,
                                        telegram_factory=FakeTelegram, runtime_factory=FakeRuntime, realtime_factory=FakeRealtime)

    async def asyncTearDown(self):
        await self.api.aclose()

    async def test_persistent_clients_control_server_and_clean_shutdown(self):
        stop = asyncio.Event()
        started = asyncio.Event()

        async def sync():
            started.set()
            return {"state": "disabled"}

        self.service.telegram.sync_once.side_effect = sync
        task = asyncio.create_task(self.service.run(stop))
        await asyncio.wait_for(started.wait(), 1)
        stop.set()
        await asyncio.wait_for(task, 1)
        self.service.telegram.start_control_server.assert_awaited_once()
        self.service.telegram.disconnect.assert_awaited_once()
        self.assertEqual("http://api.invalid/internal/telegram", self.service.telegram.options["api_url"])
        self.assertTrue(self.service.realtime.cancelled)
        self.assertEqual("stopped", self.service.status["leyu"]["state"])
        self.assertIs(self.service.telegram.source_health, self.service.status)

    async def test_leyu_failure_does_not_block_telegram_loop(self):
        stop = asyncio.Event()
        failed = asyncio.Event()

        async def collect():
            failed.set()
            raise LeyuError("upstream_timeout")

        self.service.runtime.collect.side_effect = collect

        async def sync():
            await failed.wait()
            stop.set()
            return {"state": "disabled"}

        self.service.telegram.sync_once.side_effect = sync
        await asyncio.wait_for(self.service.run(stop), 1)
        self.assertEqual("upstream_timeout", self.service.status["leyu"]["last_error"])
        self.service.telegram.sync_once.assert_awaited_once()

    async def test_telegram_failure_does_not_block_leyu_upload(self):
        stop = asyncio.Event()
        self.service.telegram.sync_once.side_effect = RuntimeError("secret-body")

        async def collect():
            await asyncio.sleep(0)
            stop.set()
            return [{"id": "leyu:1", "markets": []}]

        self.service.runtime.collect.side_effect = collect
        await asyncio.wait_for(self.service.run(stop), 1)
        self.assertEqual(1, len(self.posts))
        self.assertEqual("RuntimeError", self.service.status["telegram"]["last_error"])
        self.assertNotIn("secret-body", str(self.service.status))

    async def test_external_cancellation_closes_telegram_and_all_children(self):
        stop = asyncio.Event()
        ready = asyncio.Event()

        async def sync():
            ready.set()
            await asyncio.Event().wait()

        self.service.telegram.sync_once.side_effect = sync
        task = asyncio.create_task(self.service.run(stop))
        await asyncio.wait_for(ready.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.service.telegram.disconnect.assert_awaited_once()
        self.assertTrue(self.service.realtime.cancelled)

    async def test_component_crash_is_not_silently_healthy(self):
        self.service.realtime.run = AsyncMock(side_effect=RuntimeError("component-test-failure"))
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(self.service.run(asyncio.Event()), 1)
        self.service.telegram.disconnect.assert_awaited_once()

    async def test_upload_batches_by_both_fixture_and_quote_budget(self):
        fixtures = [{"id": str(n), "markets": [{}] * 40} for n in range(121)]
        self.assertEqual(121, await self.service.upload(fixtures))
        self.assertEqual([50, 50, 21], [len(batch) for batch in self.posts])
        more = list(fixture_batches([{"id": str(n), "markets": [{}] * 100} for n in range(25)]))
        self.assertEqual([20, 5], [len(batch) for batch in more])

    async def test_unknown_push_schedules_http_without_awaiting_full_resync(self):
        await self.service.on_push([{"fixture_id": "leyu:unknown"}])
        self.assertTrue(self.service.resync.is_set())
        self.service.runtime.collect.assert_not_called()

    async def test_stop_and_wake_cancel_waiters_without_leak(self):
        stop, wake = asyncio.Event(), asyncio.Event()
        wake.set()
        before = len(asyncio.all_tasks())
        await wait_for_work(stop, 60, wake)
        self.assertFalse(wake.is_set())
        self.assertEqual(before, len(asyncio.all_tasks()))

    async def test_invalid_config_and_failed_ingestion_are_visible(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"items": []}))) as api:
            with self.assertRaises(LeyuError):
                await get_config(api, "https://api.invalid/internal")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as api:
            self.service.api = api
            with self.assertRaises(httpx.HTTPStatusError):
                await self.service.upload([{"id": "leyu:1", "markets": []}])


class CollectionBudgetTests(unittest.TestCase):
    def test_oversized_market_snapshot_never_silently_truncated(self):
        with self.assertRaises(LeyuError):
            list(fixture_batches([{"id": "1", "markets": [{}] * 501}]))

    def test_stale_and_missing_source_times_not_marked_fresh(self):
        now = datetime(2026, 10, 26, tzinfo=timezone.utc)
        quotes = [{"status": "active", "source_updated_at": (now - timedelta(seconds=5)).isoformat()},
                  {"status": "active", "source_updated_at": (now - timedelta(seconds=120)).isoformat()},
                  {"status": "active", "source_updated_at": None},
                  {"status": "suspended", "source_updated_at": now.isoformat()}]
        result = quote_freshness([{"markets": quotes}], now=now)
        self.assertEqual("partial", result["freshness"])
        self.assertEqual((1, 1, 2), (result["fresh_active_quotes"], result["unknown_time_active_quotes"], result["known_time_active_quotes"]))
        self.assertEqual(120, result["oldest_quote_age_seconds"])
        self.assertEqual("unknown", quote_freshness([{"markets": []}], now=now)["freshness"])


if __name__ == "__main__":
    unittest.main()
