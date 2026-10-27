import asyncio
import base64
from datetime import datetime, timezone
import gzip
import json
import unittest
from unittest.mock import AsyncMock

from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from services.collector.leyu_protocol import AuthExpired, LeyuError
from services.collector.leyu_realtime import LeyuRealtime, push_events, subscription_frames


def snapshot(**updates):
    return {"mid": "5714088", "ms": 1, "time": "1790923618643", "hls2": {"4": [
        {"chpid": "4", "hid": "market", "hv": "-0.5/1", "ol": [
            {"oid": "home", "ot": "1", "ov": 211000, "obv": 211000},
            {"oid": "away", "ot": "2", "ov": 178000, "obv": 178000}]}]}, **updates}


class RealtimeProtocolTests(unittest.TestCase):
    def test_subscription_batches_cover_all_fixtures_and_deduplicate(self):
        frames = subscription_frames([str(n) for n in range(1868)] + ["1"])
        self.assertEqual(10, len(frames))
        self.assertEqual(1868, sum(len(frame["list"]) for frame in frames))
        self.assertEqual("1867", frames[-1]["list"][-1]["mid"])
        self.assertTrue(all(len(frame["list"]) <= 200 for frame in frames))

    def test_compressed_snapshot_all_aliases_preserve_source_time_and_unknown_status(self):
        encoded = base64.b64encode(gzip.compress(json.dumps(snapshot()).encode())).decode()
        for command in ("C105", "C2", "C21"):
            event = push_events({"cmd": command, "cd": encoded}, received_at=datetime(2026, 10, 26, tzinfo=timezone.utc))[0]
            self.assertFalse(event["requires_resync"])
            self.assertEqual("leyu:5714088", event["fixture_id"])
            self.assertEqual(2, len(event["markets"]))
            quote = event["markets"][0]
            self.assertEqual(("2.11", "-0.75", "unknown"), (quote["price"], quote["line"], quote["score_basis"]))
            self.assertEqual("known", quote["source_time_quality"])
            self.assertFalse(quote["status_evidence"])
            self.assertEqual("suspended", quote["status"])
            self.assertNotEqual(event["received_at"], quote["source_updated_at"])

    def test_explicit_status_and_source_absence(self):
        row = snapshot(time=None)
        block = row["hls2"]["4"][0]
        block["hs"] = 0
        block["ol"][0]["os"] = 1
        quote = push_events({"cmd": "C105", "cd": row})[0]["markets"][0]
        self.assertTrue(quote["status_evidence"])
        self.assertEqual("active", quote["status"])
        self.assertIsNone(quote["source_updated_at"])

    def test_scores_and_finish_are_exact_period_and_unknown_shape_resync(self):
        event = push_events({"cmd": "C103", "cd": {"mid": "1", "msc": "S10|8:8,S1|2:1,S2|1:0"}})[0]
        self.assertEqual({"home_score": 2, "away_score": 1, "half_home_score": 1, "half_away_score": 0}, event["fixture_patch"])
        self.assertIsNone(event["source_updated_at"])
        self.assertEqual("finished", push_events({"cmd": "C102", "cd": {"mid": "1", "mmp": "999"}})[0]["fixture_patch"]["phase"])
        self.assertTrue(push_events({"cmd": "C104", "cd": {"mid": "1", "unknown": 0}})[0]["requires_resync"])
        self.assertTrue(push_events({"cmd": "C105", "cd": {"unknown": 0}})[0]["requires_resync"])
        self.assertEqual([], push_events({"cmd": "unknown", "cd": {}}))

    def test_only_explicit_auth_code_is_expired(self):
        with self.assertRaises(AuthExpired):
            push_events({"code": "0401013"})
        with self.assertRaises(LeyuError) as raised:
            push_events({"code": "0401015"})
        self.assertNotIsInstance(raised.exception, AuthExpired)


class RealtimeLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_403_handshake_backs_off_and_keeps_collector_alive(self):
        stop = asyncio.Event()
        attempts = 0

        def connector(url, **kwargs):
            nonlocal attempts
            attempts += 1
            stop.set()
            raise InvalidStatus(Response(403, "Forbidden", Headers()))

        service = LeyuRealtime(lambda: {"leyu_api_base": "https://business.invalid", "leyu_request_id": "authorized"},
                               lambda: [], AsyncMock(), AsyncMock(), connector=connector)
        await service.run(stop)
        self.assertEqual(1, attempts)
        self.assertEqual(1, service.stats["reconnects"])
        self.assertEqual("websocket_http_403", service.stats["last_error"])
        self.assertEqual("stopped", service.stats["state"])

    async def test_changed_subscriptions_unsubscribe_then_replace(self):
        values = ["1", "2"]
        socket = type("Socket", (), {"send": AsyncMock()})()
        service = LeyuRealtime(lambda: {}, lambda: values, AsyncMock(), AsyncMock())
        previous = await service._subscriptions(socket, {})
        self.assertEqual(2, service.stats["subscription_requests_sent"])
        values[:] = ["2", "3"]
        await service._subscriptions(socket, previous)
        self.assertEqual(2, service.stats["subscription_requests_sent"])
        sent = [json.loads(args.args[0]) for args in socket.send.await_args_list]
        self.assertEqual("1", sent[1]["cclose"])
        self.assertEqual(["2", "3"], [row["mid"] for row in sent[2]["list"]])

    async def test_partial_send_reports_only_dispatch_coverage_not_upstream_confirmation(self):
        socket = type("Socket", (), {"send": AsyncMock(side_effect=[None, OSError("private-token")])})()
        service = LeyuRealtime(lambda: {}, lambda: [str(i) for i in range(450)], AsyncMock(), AsyncMock())
        with self.assertRaises(OSError):
            await service._subscriptions(socket, {})
        self.assertEqual((450, 200, 250), (service.stats["subscription_expected"],
                         service.stats["subscription_requests_sent"], service.stats["subscription_send_missing"]))
        self.assertAlmostEqual(250 / 450, service.stats["subscription_send_missing_ratio"], places=6)
        self.assertIsNone(service.stats["subscription_miss_ratio"])
        self.assertIsNone(service.stats["subscription_confirmed_fixtures"])
        self.assertEqual("unverified", service.stats["subscription_confirmation_state"])
        self.assertNotIn("private-token", str(service.stats))
        socket.send = AsyncMock()
        await service._subscriptions(socket, {})
        self.assertEqual((450, 0, 0), (service.stats["subscription_requests_sent"],
                         service.stats["subscription_send_missing"], service.stats["subscription_send_missing_ratio"]))

    async def test_subscription_threshold_has_one_failure_and_one_recovery_transition(self):
        service = LeyuRealtime(lambda: {}, lambda: ["1"], AsyncMock(), AsyncMock())
        socket = type("Socket", (), {"send": AsyncMock(side_effect=OSError("network"))})()
        for _ in range(4):
            with self.assertRaises(OSError):
                await service._subscriptions(socket, {})
        self.assertEqual(("active", 1), (service.stats["subscription_alert_state"],
                         service.stats["subscription_alert_transitions"]))
        socket.send = AsyncMock()
        await service._subscriptions(socket, {})
        self.assertEqual("recovering", service.stats["subscription_alert_state"])
        await service._subscriptions(socket, {})
        self.assertEqual(("clear", 2, 1), (service.stats["subscription_alert_state"],
                         service.stats["subscription_alert_transitions"], service.stats["subscription_alert_recoveries"]))

    async def test_websocket_429_retains_bounded_cooldown_and_safe_retry_diagnostics(self):
        stop = asyncio.Event()
        def connector(url, **kwargs):
            stop.set()
            raise InvalidStatus(Response(429, "Too Many Requests", Headers({"Retry-After": "999999"})))
        service = LeyuRealtime(lambda: {"leyu_api_base": "https://business.invalid", "leyu_request_id": "authorized"},
                               lambda: [], AsyncMock(), AsyncMock(), connector=connector)
        await service.run(stop)
        self.assertEqual(("rate_limit", "429", 900, 1),
                         (service.stats["failure_group"], service.stats["last_error_code"],
                          service.stats["backoff_seconds"], service.stats["rate_limit_streak"]))
        self.assertTrue(service.stats["retry_at"])

    async def test_rate_limit_and_permission_push_are_not_swallowed_as_malformed_message(self):
        stop = asyncio.Event()
        class Socket:
            send = AsyncMock()
            async def recv(self):
                stop.set()
                return json.dumps({"code": code, "msg": "private-response-token"})
        class Connection:
            async def __aenter__(self):
                return Socket()
            async def __aexit__(self, *args):
                pass
        for code, category, group in (("0401038", "upstream_rate_limited", "rate_limit"),
                                      ("0401015", "permission_denied", "permission")):
            with self.subTest(code=code):
                stop = asyncio.Event()
                service = LeyuRealtime(lambda: {"leyu_api_base": "https://business.invalid", "leyu_request_id": "authorized"},
                                       lambda: ["1"], AsyncMock(), AsyncMock(), connector=lambda *args, **kwargs: Connection())
                await service.run(stop)
                self.assertEqual((category, code, group),
                                 (service.stats["last_error"], service.stats["last_error_code"], service.stats["failure_group"]))
                if group == "rate_limit":
                    self.assertEqual(60, service.stats["backoff_seconds"])
                self.assertEqual(0, service.stats["subscription_requests_sent"])
                self.assertEqual(1, service.stats["subscription_send_missing"])
                self.assertNotIn("private-response", str(service.stats))

    async def test_normal_socket_has_no_proxy_and_shutdown_closes(self):
        stop = asyncio.Event()
        sent = []
        options = {}

        class Socket:
            async def send(self, value):
                sent.append(json.loads(value))
            async def recv(self):
                stop.set()
                return json.dumps({"cmd": "C0"})

        class Connection:
            async def __aenter__(self):
                return Socket()
            async def __aexit__(self, *args):
                options["closed"] = True

        def connector(url, **kwargs):
            options.update(kwargs)
            options["url"] = url
            return Connection()

        service = LeyuRealtime(lambda: {"leyu_api_base": "https://business.invalid", "leyu_request_id": "authorized"},
                               lambda: ["1"], AsyncMock(), AsyncMock(), connector=connector)
        await service.run(stop)
        self.assertIsNone(options["proxy"])
        self.assertTrue(options["closed"])
        self.assertTrue(options["url"].startswith("wss://business.invalid/"))
        self.assertEqual(["C8", "C0"], [message["cmd"] for message in sent])
        self.assertEqual("stopped", service.stats["state"])

    async def test_consumer_keeps_independent_updates_and_gap_resync(self):
        stop = asyncio.Event()
        delivered = []

        async def on_batch(events):
            delivered.extend(events)
            stop.set()

        resync = AsyncMock()
        service = LeyuRealtime(lambda: {}, lambda: [], on_batch, resync)
        service._gap = True
        for score in ("S1|1:0", "S2|1:0"):
            service.queue.put_nowait(push_events({"cmd": "C103", "cd": {"mid": "1", "msc": score}})[0])
        await service._consume(stop)
        self.assertEqual(2, len(delivered))
        self.assertEqual(1, resync.await_count)
        self.assertEqual(0, service.queue.qsize())

    async def test_delivery_failure_retries_same_batch_and_stays_alive(self):
        stop = asyncio.Event()
        attempts = []

        async def on_batch(events):
            attempts.append(events)
            if len(attempts) == 1:
                raise RuntimeError("private-response-must-not-leak")
            stop.set()

        service = LeyuRealtime(lambda: {}, lambda: [], on_batch, AsyncMock())
        service.queue.put_nowait({"fixture_id": "leyu:1", "payload": {"mid": "1"}})
        await service._consume(stop)
        self.assertEqual(2, len(attempts))
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(1, service.stats["delivery_failures"])
        self.assertNotIn("private-response", str(service.stats))
        self.assertEqual(0, service.queue._unfinished_tasks)

    async def test_latency_queue_and_delivery_metrics_use_bounded_scalar_samples(self):
        now = datetime.now(timezone.utc)
        source_time = int((now.timestamp() - 2) * 1000)
        event = push_events({"cmd": "C105", "cd": snapshot(time=str(source_time))}, received_at=now)[0]
        stop = asyncio.Event()
        delivered = []

        async def on_batch(events):
            delivered.extend(events)
            stop.set()

        service = LeyuRealtime(lambda: {}, lambda: [], on_batch, AsyncMock())
        service._record_event(event)
        service.queue.put_nowait(event)
        service.stats["queue_depth"] = service.queue.qsize()
        service.stats["queue_high_water"] = service.queue.qsize()
        await service._consume(stop)
        self.assertEqual(1, len(delivered))
        self.assertEqual(1, service.stats["source_lag_samples"])
        self.assertGreaterEqual(service.stats["source_lag_ms_p50"], 1500)
        self.assertEqual(1, service.stats["queue_wait_samples"])
        self.assertEqual(1, service.stats["events_delivered"])
        self.assertEqual(1, service.stats["batches_delivered"])
        self.assertEqual(1, service.stats["delivery_age_samples"])
        self.assertGreaterEqual(service.stats["delivery_age_ms_p95"], 1500)
        self.assertEqual(1, service.stats["queue_high_water"])
        self.assertEqual(0, service.stats["queue_depth"])
        self.assertTrue(service.stats["last_event_at"])
        self.assertTrue(service.stats["last_delivery_at"])

    async def test_queue_overflow_counts_drop_and_forces_resync_before_delivery(self):
        first = {"fixture_id": "leyu:1", "command": "C103", "payload": {"mid": "1", "msc": "S1|1:0"}}
        second = {"fixture_id": "leyu:1", "command": "C103", "payload": {"mid": "1", "msc": "S1|2:0"}}
        service = LeyuRealtime(lambda: {}, lambda: [], AsyncMock(), AsyncMock(), queue_size=1)
        self.assertTrue(service._enqueue_event(first))
        self.assertFalse(service._enqueue_event(second))
        self.assertEqual(1, service.stats["dropped"])
        self.assertEqual(1, service.stats["queue_high_water"])
        self.assertEqual(1, service.stats["queue_depth"])
        self.assertTrue(service._gap)
        delivered = []
        async def resync():
            delivered.append("resync")
        async def on_batch(batch):
            delivered.extend(batch)
            stop.set()
        stop = asyncio.Event()
        service.on_resync = resync
        service.on_batch = on_batch
        await service._consume(stop)
        self.assertEqual("resync", delivered[0])
        self.assertEqual([first], delivered[1:])
        self.assertEqual(0, service.stats["queue_depth"])

    async def test_identical_events_are_deduplicated_without_collapsing_distinct_updates(self):
        first = {"fixture_id": "leyu:1", "command": "C103", "payload": {"mid": "1", "msc": "S1|1:0"}}
        second = {"fixture_id": "leyu:1", "command": "C103", "payload": {"mid": "1", "msc": "S1|2:0"}}
        delivered = []
        stop = asyncio.Event()
        async def on_batch(batch):
            delivered.extend(batch)
            stop.set()
        service = LeyuRealtime(lambda: {}, lambda: [], on_batch, AsyncMock())
        for event in (first, first.copy(), second):
            service._enqueue_event(event)
        await service._consume(stop)
        self.assertEqual([first, second], delivered)

    def test_unknown_or_future_source_time_does_not_pollute_latency_sample(self):
        service = LeyuRealtime(lambda: {}, lambda: [], AsyncMock(), AsyncMock())
        service._record_event({"received_at": datetime.now(timezone.utc).isoformat(), "source_updated_at": None})
        service._record_event({"received_at": datetime.now(timezone.utc).isoformat(),
                               "source_updated_at": "2999-01-01T00:00:00+00:00"})
        self.assertEqual(0, service.stats["source_lag_samples"])
        self.assertIsNone(service.stats["source_lag_ms_p95"])


if __name__ == "__main__":
    unittest.main()
