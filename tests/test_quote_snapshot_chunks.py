"""Collector atomic wire, bounded quarantine and delivery diagnostics contracts."""
import asyncio
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock

import httpx

from services.collector.quote_snapshot import (MAX_SNAPSHOT_BYTES, encode_snapshot, fixture_envelope,
                                               prepare_fixture_batches, snapshot_quote_count)
from services.collector.leyu_protocol import LeyuError
from services.collector.worker import CollectorService
from services.collector.leyu_realtime import LeyuRealtime
from tests.test_collector_lifecycle import FakeTelegram, FakeRuntime, FakeRealtime


def quote(index):
    return {"market_id":"4","market_name":"让球","period":"full_time","kind":"handicap",
            "line":str(index / 4),"selection":"home","price":"2.00","status":"active",
            "source_updated_at":"2026-10-27T18:00:00+00:00","received_at":"2026-10-27T18:00:00+00:00",
            "source_time_quality":"known","selection_id":str(index)}


def fixture(count,identity="leyu:one",complete=True):
    return {"id":identity,"source_id":identity,"source_match_id":identity.split(":")[-1],
            "markets":[quote(index) for index in range(count)],"markets_complete":complete,"phase":"live"}


class QuoteChunkWireTests(unittest.TestCase):
    def test_real_api_contract_accepts_all_collector_chunks_and_rejects_missing_last_chunk(self):
        from app.routes.internal import FixtureBatch
        from pydantic import ValidationError
        original={**fixture(1001),"sport":"football","competition_name":"Synthetic league",
                  "home_team":"Synthetic home","away_team":"Synthetic away",
                  "kickoff_at":"2026-10-27T18:00:00+00:00","received_at":"2026-10-27T18:00:00+00:00"}
        batches,_=prepare_fixture_batches([original])
        body=json.loads(encode_snapshot(batches[0]))
        validated=FixtureBatch.model_validate(body)
        self.assertEqual(1001,len(validated.items[0].all_markets()))
        self.assertTrue(validated.items[0].markets_complete)
        body["items"][0]["market_chunks"].pop()
        with self.assertRaises(ValidationError):FixtureBatch.model_validate(body)

    def test_501_2000_preserve_every_quote_in_exactly_one_fixture_envelope(self):
        for count,lengths in ((501,[500,1]),(1001,[500,500,1]),(2000,[500]*4)):
            with self.subTest(count=count):
                original=fixture(count);before=deepcopy(original)
                batches,report=prepare_fixture_batches([original])
                self.assertEqual(1,len(batches));self.assertEqual(1,len(batches[0]))
                envelope=batches[0][0]
                self.assertNotIn("markets",envelope)
                self.assertEqual(lengths,[len(chunk) for chunk in envelope["market_chunks"]])
                self.assertEqual(count,envelope["market_quote_count"])
                self.assertEqual(len(lengths),envelope["market_chunk_count"])
                self.assertEqual(original["markets"],[row for chunk in envelope["market_chunks"] for row in chunk])
                self.assertEqual(before,original)
                self.assertTrue(envelope["markets_complete"])
                self.assertEqual(0,report["quarantined_fixtures"])

    def test_partial_snapshot_never_acquires_complete_flag(self):
        result=fixture_envelope(fixture(501,complete=False))
        self.assertFalse(result["markets_complete"])

    def test_500_or_empty_ordinary_wire_still_supported(self):
        for count in (0,500):
            result=fixture_envelope(fixture(count))
            self.assertNotIn("market_chunks",result)
            self.assertEqual(count,len(result["markets"]))

    def test_total_batch_quote_budget_splits_without_splitting_same_fixture(self):
        batches,report=prepare_fixture_batches([fixture(501,"a"),fixture(1500,"b"),fixture(500,"c")])
        self.assertEqual([[501],[1500,500]],[[snapshot_quote_count(item) for item in batch] for batch in batches])
        self.assertEqual(0,report["quarantined_fixtures"])

    def test_over_2000_is_quarantined_without_truncation_or_starving_neighbors(self):
        too_big=fixture(2001,"large")
        batches,report=prepare_fixture_batches([fixture(4,"first"),too_big,fixture(6,"last")])
        self.assertEqual(["first","last"],[item["id"] for batch in batches for item in batch])
        self.assertEqual(2001,len(too_big["markets"]))
        self.assertEqual(1,report["quarantined_fixtures"])
        self.assertEqual(2001,report["max_observed_fixture_quotes"])
        self.assertEqual("fixture_quote_budget_exceeded",report["quarantined"][0]["reason"])

    def test_utf8_actual_body_budget_not_character_count_and_oversize_isolated(self):
        large=fixture(1,"large");large["untrusted_text"]="中"*(MAX_SNAPSHOT_BYTES//3)
        batches,report=prepare_fixture_batches([large,fixture(1,"good")])
        self.assertEqual(["good"],[item["id"] for batch in batches for item in batch])
        self.assertEqual("fixture_payload_budget_exceeded",report["quarantined"][0]["reason"])
        with self.assertRaises(LeyuError):encode_snapshot([large])

    def test_byte_budget_can_split_small_quote_count_fixtures(self):
        rows=[{**fixture(1,str(index)),"padding":"x"*1500000} for index in range(3)]
        batches,_=prepare_fixture_batches(rows)
        self.assertEqual([2,1],[len(batch) for batch in batches])
        for batch in batches:self.assertLessEqual(len(encode_snapshot(batch)),MAX_SNAPSHOT_BYTES)


class ChunkUploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests=[]
        def handler(request):
            self.requests.append(request)
            return httpx.Response(200,json={"accepted":len(json.loads(request.content)["items"])})
        self.api=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.service=CollectorService(self.api,"http://api.invalid/internal","fixture-token",
                                      telegram_factory=FakeTelegram,runtime_factory=FakeRuntime,realtime_factory=FakeRealtime)

    async def asyncTearDown(self):
        await self.service.leyu_refresh.close();await self.api.aclose()

    async def test_all_chunks_uploaded_single_http_request_with_one_complete_flag(self):
        self.assertEqual(1,await self.service.upload([fixture(1001)]))
        self.assertEqual(1,len(self.requests))
        request=self.requests[0]
        self.assertEqual("application/json",request.headers["content-type"])
        body=json.loads(request.content)
        self.assertEqual([500,500,1],[len(chunk) for chunk in body["items"][0]["market_chunks"]])
        self.assertTrue(body["items"][0]["markets_complete"])

    async def test_over_budget_does_not_raise_on_push_or_suppress_other_fixtures(self):
        self.service.runtime.apply_push=lambda events:([fixture(2001,"bad"),fixture(1,"good")],False)
        with self.assertLogs("platform.collector",level="WARNING") as logs:
            await self.service.on_push([])
        self.assertEqual(1,len(self.requests))
        self.assertEqual(["good"],[item["id"] for item in json.loads(self.requests[0].content)["items"]])
        self.assertEqual(1,self.service.status["leyu"]["last_push_quarantined_fixtures"])
        self.assertEqual(2001,self.service.status["leyu"]["max_observed_fixture_quotes"])
        self.assertNotIn("fixture_id=bad",str(logs.output))

    async def test_only_quarantined_fixture_is_not_reported_as_written_or_sent(self):
        with self.assertLogs("platform.collector",level="WARNING"):
            accepted=await self.service.upload([fixture(2001)])
        self.assertEqual(0,accepted)
        self.assertEqual([],self.requests)

    async def test_selected_odds_exclusion_does_not_send_or_quarantine_unrequested_large_projection(self):
        self.assertEqual(2,await self.service.upload([fixture(2001,"bad"),fixture(1,"good")],odds_mids=["good"]))
        self.assertEqual(0,self.service.last_upload_report["quarantined_fixtures"])
        body=json.loads(self.requests[0].content)
        self.assertEqual([],body["items"][0]["markets"])
        self.assertFalse(body["items"][0]["markets_complete"])


class SafeDeliveryDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_delivery_exception_category_logged_without_payload_or_response(self):
        stop=asyncio.Event()
        calls=[]
        async def on_batch(events):
            calls.append(events)
            if len(calls)==1:raise LeyuError("fixture_payload_budget_exceeded")
            stop.set()
        service=LeyuRealtime(lambda:{},lambda:[],on_batch,AsyncMock())
        await service.queue.put({"fixture_id":"leyu:one","received_at":"2026-10-27T18:00:00+00:00","markets":[]})
        with self.assertLogs("platform.collector.websocket",level="WARNING") as logs:
            await asyncio.wait_for(service._consume(stop),1)
        self.assertEqual(1,service.stats["delivery_failures"])
        self.assertEqual("fixture_payload_budget_exceeded",service.stats["last_delivery_error_category"])
        self.assertIn("category=fixture_payload_budget_exceeded",logs.output[0])
        self.assertNotIn("leyu:one",logs.output[0])
