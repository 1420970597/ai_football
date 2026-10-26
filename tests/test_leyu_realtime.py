import asyncio
import base64
from datetime import datetime, timezone
import gzip
import json
import unittest
from unittest.mock import AsyncMock

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
    async def test_changed_subscriptions_unsubscribe_then_replace(self):
        values = ["1", "2"]
        socket = type("Socket", (), {"send": AsyncMock()})()
        service = LeyuRealtime(lambda: {}, lambda: values, AsyncMock(), AsyncMock())
        previous = await service._subscriptions(socket, {})
        values[:] = ["2", "3"]
        await service._subscriptions(socket, previous)
        sent = [json.loads(args.args[0]) for args in socket.send.await_args_list]
        self.assertEqual("1", sent[1]["cclose"])
        self.assertEqual(["2", "3"], [row["mid"] for row in sent[2]["list"]])

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


if __name__ == "__main__":
    unittest.main()
