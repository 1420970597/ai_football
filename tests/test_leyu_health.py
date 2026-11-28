import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import socket
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from services.collector.leyu import LeyuClient, DEFAULT_FIXTURE_PATH, DEFAULT_ODDS_PATH, retry_after_seconds
from services.collector.leyu_health import ThresholdAlert
from services.collector.leyu_protocol import AuthExpired, LeyuError, RateLimited
from services.collector.leyu_refresh import LeyuRequestBudget
from services.collector.worker import CollectorService, collection_retry_delay, failure_group
from tests.test_collector_lifecycle import FakeRuntime, FakeRealtime, FakeTelegram


class DiagnosticPolicyTests(unittest.TestCase):
    def test_retry_after_is_bounded_and_rejects_untrusted_values(self):
        now = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
        self.assertEqual(18, retry_after_seconds("17.2"))
        self.assertEqual(900, retry_after_seconds("99999999"))
        self.assertEqual(120, retry_after_seconds(format_datetime(now + timedelta(seconds=120)), now=now))
        self.assertEqual(0, retry_after_seconds(format_datetime(now - timedelta(seconds=120)), now=now))
        for value in (None, "", "-1", "NaN", "inf", "private-response-token", "9" * 129):
            with self.subTest(value=value):
                self.assertIsNone(retry_after_seconds(value))

    def test_consecutive_limits_back_off_without_exceeding_budget(self):
        self.assertEqual([60, 120, 240, 480, 900, 900],
                         [collection_retry_delay(20, 0, rate_limit_streak=step) for step in range(1, 7)])
        self.assertEqual(900, collection_retry_delay(20, 1, rate_limit_streak=1, retry_after=999999))
        self.assertEqual(180, collection_retry_delay(20, 1, rate_limit_streak=1, retry_after=180))
        self.assertEqual(60, collection_retry_delay(20, 1, rate_limit_streak=1, retry_after=-100))
        self.assertEqual(20, collection_retry_delay(20, 0))
        self.assertEqual(2, collection_retry_delay(20, 1))

    def test_error_families_do_not_confuse_ingestion_with_source(self):
        for error, expected in [(RateLimited("upstream_rate_limited"), "rate_limit"),
                                (AuthExpired("session_expired"), "session"),
                                (LeyuError("http_access_denied"), "permission"),
                                (LeyuError("invalid_json_response"), "protocol"),
                                (LeyuError("invalid_collection_budget"), "configuration"),
                                (LeyuError("upstream_domain_unresolved"), "domain"),
                                (LeyuError("upstream_timeout"), "network")]:
            self.assertEqual(expected, failure_group(error))
            self.assertEqual("ingestion", failure_group(error, stage="ingestion"))

    def test_alert_requires_consecutive_failures_and_recovery_samples(self):
        alert = ThresholdAlert(.8, .25)
        self.assertFalse(alert.observe(None))
        self.assertEqual("unknown", alert.state)
        self.assertFalse(alert.observe(1))
        self.assertTrue(alert.observe(1))
        for _ in range(100):
            self.assertFalse(alert.observe(1))
        self.assertEqual(1, alert.transitions)
        self.assertFalse(alert.observe(0))
        self.assertFalse(alert.observe(float("nan")))
        self.assertTrue(alert.active)
        self.assertFalse(alert.observe(0))
        self.assertTrue(alert.observe(0))
        self.assertEqual((False, "clear", 2, 1), (alert.active, alert.state, alert.transitions, alert.recoveries))
        self.assertTrue(alert.fields("queue_alert")["queue_alert_recovered_at"])


class HttpCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_and_envelope_limits_keep_safe_operation_and_header_cooldown(self):
        for status, body in ((429, {}), (200, {"code": "0401038", "msg": "private-response-token"})):
            with self.subTest(status=status):
                client = LeyuClient("https://upstream.invalid", request_id="secret-session")
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                        lambda request: httpx.Response(status, json=body, headers={"Retry-After": "180"}))) as http:
                    with self.assertRaises(RateLimited) as caught:
                        await client._request(http, "POST", DEFAULT_ODDS_PATH, json={})
                error = caught.exception
                self.assertEqual(("odds_batch", status, 180),
                                 (error.operation, error.http_status, error.retry_after_seconds))
                self.assertNotIn("private-response", str(error))
                self.assertNotIn("secret-session", str(error))

    async def test_missing_header_and_transport_failure_are_explicit(self):
        client = LeyuClient("https://upstream.invalid", request_id="test-only")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(429))) as http:
            with self.assertRaises(RateLimited) as caught:
                await client._request(http, "POST", DEFAULT_FIXTURE_PATH)
        self.assertIsNone(caught.exception.retry_after_seconds)
        self.assertEqual("fixture_snapshot", caught.exception.operation)
        def timeout(request):
            raise httpx.ReadTimeout("private-token", request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
            with self.assertRaises(LeyuError) as caught:
                await client._request(http, "POST", DEFAULT_FIXTURE_PATH)
        self.assertEqual(("upstream_timeout", "fixture_snapshot", None),
                         (caught.exception.category, caught.exception.operation, caught.exception.http_status))
        self.assertNotIn("private-token", str(caught.exception))
        def dns_failure(request):
            try:
                raise socket.gaierror(-2, "private-hostname")
            except socket.gaierror as cause:
                raise httpx.ConnectError("private-url", request=request) from cause
        async with httpx.AsyncClient(transport=httpx.MockTransport(dns_failure)) as http:
            with self.assertRaises(LeyuError) as caught:
                await client._request(http, "POST", DEFAULT_FIXTURE_PATH)
        self.assertEqual("upstream_domain_unresolved", caught.exception.category)
        self.assertNotIn("private", str(caught.exception))

    async def test_worker_keeps_cooldown_on_push_and_recovers_after_three_good_polls(self):
        async with httpx.AsyncClient() as api:
            service = CollectorService(api, "http://api.invalid/internal", "test-only", interval=20,
                telegram_factory=FakeTelegram, runtime_factory=FakeRuntime, realtime_factory=FakeRealtime)
            # This scenario assumes the normal config loop has acknowledged the
            # default quote policy; it tests source cooldown, not missing config.
            service.quote_max_age_seconds = 15
            service.quote_policy_available = True
            # A no-op wait cannot satisfy a real monotonic cooldown. Advance one
            # shared fake clock instead, retaining the production cooldown gate.
            clock = [0.0]
            budget = LeyuRequestBudget(clock=lambda: clock[0])
            service.runtime.request_budget = budget
            service.leyu_refresh.budget = budget
            stop = asyncio.Event()
            rounds = []
            async def collect(**kwargs):
                if service.status["leyu"]["poll_attempts"] == 1:
                    service.resync.set()
                    raise RateLimited("http_rate_limited", "429", operation="fixture_snapshot", http_status=429)
                if service.status["leyu"]["poll_attempts"] == 2:
                    service.resync.set()
                    service.runtime.last_report = {"rate_limited": True, "rate_limit_code": "0401038",
                        "rate_limit_category": "upstream_rate_limited", "rate_limit_operation": "odds_batch",
                        "rate_limit_http_status": 200, "odds_requested_fixtures": 1, "complete_market_fixtures": 0}
                else:
                    service.runtime.last_report = {"odds_requested_fixtures": 0, "complete_market_fixtures": 0}
                return []
            service.runtime.collect.side_effect = collect
            async def wait(stop_event, delay, wake=None):
                if service.status["leyu"]["poll_attempts"] > len(rounds):
                    rounds.append((delay, wake, dict(service.status["leyu"])))
                    if len(rounds) == 5:
                        stop_event.set()
                clock[0] += max(0, delay)
            with patch("services.collector.worker.wait_for_work", side_effect=wait):
                await service.leyu_loop(stop)
            self.assertEqual([(60, None), (120, None)], [(delay, wake) for delay, wake, _ in rounds[:2]])
            self.assertEqual("fixture_snapshot", rounds[0][2]["last_operation"])
            self.assertEqual("active", rounds[1][2]["poll_alert_state"])
            status = service.status["leyu"]
            self.assertEqual((5, 4, 1), (status["poll_attempts"], status["poll_successes"], status["poll_failures"]))
            self.assertEqual(("healthy", "clear", 1, 0),
                             (status["state"], status["poll_alert_state"], status["poll_alert_recoveries"], status["rate_limit_streak"]))
            self.assertGreater(datetime.fromisoformat(status["retry_at"]), datetime.fromisoformat(status["last_poll_at"]))
            self.assertTrue(status["last_complete_at"])

    async def test_worker_preserves_partial_protocol_failure_and_ingestion_http_status(self):
        for fail_ingestion in (False, True):
            with self.subTest(fail_ingestion=fail_ingestion):
                async with httpx.AsyncClient() as api:
                    service = CollectorService(api, "http://api.invalid/internal", "test-only", interval=20,
                        telegram_factory=FakeTelegram, runtime_factory=FakeRuntime, realtime_factory=FakeRealtime)
                    service.runtime.last_report = {"odds_requested_fixtures": 1, "complete_market_fixtures": 0,
                        "failed_batches": [{"category": "invalid_odds_rows", "code": "",
                                            "operation": "odds_batch", "http_status": 200}]}
                    service.runtime.collect.return_value = [{"id": "leyu:1", "markets": []}]
                    if fail_ingestion:
                        request = httpx.Request("POST", "http://api.invalid/internal/collector/fixtures")
                        response = httpx.Response(503, request=request)
                        service.upload = AsyncMock(side_effect=httpx.HTTPStatusError("private-db-error", request=request, response=response))
                    else:
                        service.upload = AsyncMock(return_value=1)
                    stop = asyncio.Event()
                    async def wait(stop_event, delay, wake=None):
                        if service.status["leyu"]["poll_attempts"]:
                            stop_event.set()
                    with patch("services.collector.worker.wait_for_work", side_effect=wait):
                        await service.leyu_loop(stop)
                    status = service.status["leyu"]
                    self.assertEqual("ingestion" if fail_ingestion else "protocol", status["failure_group"])
                    self.assertEqual(503 if fail_ingestion else 200, status["last_http_status"])
                    self.assertEqual("ingestion" if fail_ingestion else "odds_batch", status["last_operation"])
                    self.assertEqual("HTTPStatusError" if fail_ingestion else "invalid_odds_rows", status["last_error"])
                    self.assertNotIn("private-db", str(status))
