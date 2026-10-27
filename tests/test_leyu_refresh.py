"""Isolated single-match refresh contracts; no provider or production requests."""
import asyncio
import base64
from copy import deepcopy
import gzip
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from services.collector.leyu import LeyuClient, DEFAULT_MATCH_ODDS_PATH
from services.collector.leyu_protocol import AuthExpired, LeyuError, RateLimited, parse_fixtures
from services.collector.leyu_refresh import LeyuRefreshController, LeyuRequestBudget
from services.collector.leyu_runtime import LeyuRuntime
from services.collector.telegram_control import ControlServer
from tests.test_leyu_protocol import market_row
from tests.test_collector_lifecycle import FakeTelegram, FakeRealtime
from services.collector.worker import CollectorService


def fixture(mid="m1", *, phase="live"):
    return {"id": "leyu:" + mid, "source_id": "leyu:" + mid, "source_match_id": mid,
            "phase": phase, "markets": [], "markets_complete": True}


class SingleMatchClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_detail_request_reuses_headers_relay_decode_and_source_time(self):
        requests = []
        def handle(request):
            requests.append(request)
            encoded = base64.b64encode(gzip.compress(json.dumps({"data": [market_row()]}).encode())).decode()
            return httpx.Response(200, json={"code": "0000000", "data": encoded})
        client = LeyuClient("https://business.invalid", request_id="sample-session", cookie="sample=cookie",
                            collection_relay="https://relay.invalid", transport=httpx.MockTransport(handle))
        original = parse_fixtures([market_row()])[0]
        original["competition_name"] = "schedule identity"
        result = await client.fetch_match("m1", fixture=original)
        self.assertEqual(1, len(requests))
        request = requests[0]
        self.assertEqual("POST", request.method)
        self.assertEqual("/business.invalid" + DEFAULT_MATCH_ODDS_PATH, request.url.path)
        self.assertEqual("https://business.invalid", request.headers["origin"])
        self.assertEqual(("sample-session", "sample=cookie"), (request.headers["requestid"], request.headers["cookie"]))
        self.assertEqual("m1", json.loads(request.content)["mid"])
        self.assertEqual("schedule identity", result["competition_name"])
        self.assertTrue(result["markets_complete"])
        self.assertEqual("2.11", result["markets"][0]["price"])
        self.assertNotEqual(result["markets"][0]["received_at"], result["markets"][0]["source_updated_at"])
        self.assertNotIn("markets", original)

    async def test_unknown_source_time_stays_unknown(self):
        row = market_row()
        del row["hpsData"][0]["hps"][0]["ctsp"]
        client = LeyuClient("https://business.invalid", request_id="session", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"code": "0000000", "data": [row]})))
        result = await client.fetch_match("m1", fixture=fixture())
        self.assertIsNone(result["markets"][0]["source_updated_at"])
        self.assertEqual("unknown", result["markets"][0]["source_time_quality"])

    async def test_missing_duplicate_and_incomplete_details_are_not_success(self):
        for rows, reason in [([], "fixture_detail_missing"), ([market_row(),market_row()], "ambiguous_fixture_detail"),
                              ([{"mid":"m1"}], "market_container_missing")]:
            with self.subTest(reason=reason):
                client = LeyuClient("https://business.invalid", request_id="session", transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json={"code":"0000000","data":rows})))
                with self.assertRaises(LeyuError) as raised:
                    await client.fetch_match("m1", fixture=fixture())
                self.assertEqual(reason, raised.exception.category)

    async def test_auth_and_rate_limit_are_single_attempts(self):
        for response, error in [(httpx.Response(429,headers={"Retry-After":"73"}),RateLimited),
                                  (httpx.Response(200,json={"code":"0401013"}),AuthExpired)]:
            requests=[]
            def handle(request):
                requests.append(request)
                return response
            client=LeyuClient("https://business.invalid",request_id="session",transport=httpx.MockTransport(handle))
            with self.assertRaises(error):
                await client.fetch_match("m1",fixture=fixture())
            self.assertEqual(1,len(requests))

    async def test_url_or_other_identity_rejected_before_transport(self):
        handler=AsyncMock()
        client=LeyuClient("https://business.invalid",request_id="session",transport=httpx.MockTransport(handler))
        for mid in ("https://evil.invalid/", "m1?token=x", "../m1", "", "m2"):
            with self.assertRaises(LeyuError):
                await client.fetch_match(mid,fixture=fixture())
        handler.assert_not_called()


class RuntimeMatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api=httpx.AsyncClient()
        self.runtime=LeyuRuntime(self.api,"https://api.invalid/internal")
        self.runtime.load=AsyncMock(return_value={})
        self.runtime.fixtures={"leyu:m1":fixture(),"leyu:m2":fixture("m2")}

    async def asyncTearDown(self):
        await self.api.aclose()

    async def test_refresh_only_one_match_keeps_other_projection_and_monotonic_ws(self):
        old={"market_id":"4","period":"full_time","line":"0","selection":"home","price":"2.00",
             "status":"active","source_updated_at":"2026-10-27T10:00:00+00:00"}
        self.runtime.fixtures["leyu:m1"]["markets"]=[old]
        newer={**old,"price":"2.20","source_updated_at":"2026-10-27T10:00:05+00:00","status_evidence":True}
        async def fetch(mid,**kwargs):
            self.runtime.apply_push([{"fixture_id":"leyu:m1","markets":[newer]}])
            return {**fixture(),"markets":[old]}
        client=SimpleNamespace(fetch_match=AsyncMock(side_effect=fetch))
        with patch("services.collector.leyu_runtime.LeyuClient.from_config",return_value=client):
            result=await self.runtime.refresh_match("m1")
        self.assertEqual("2.20",result["markets"][0]["price"])
        self.assertEqual({"leyu:m1","leyu:m2"},set(self.runtime.fixtures))
        client.fetch_match.assert_awaited_once()

    async def test_busy_poll_wait_is_bounded_before_config_or_provider(self):
        await self.runtime._collection_lock.acquire()
        try:
            with self.assertRaises(LeyuError) as raised:
                await self.runtime.refresh_match("m1",lock_timeout_seconds=.01)
            self.assertEqual("refresh_busy",raised.exception.category)
            self.runtime.load.assert_not_called()
        finally:
            self.runtime._collection_lock.release()

    async def test_waiting_refresh_precedes_next_poll_without_cancelling_current_poll(self):
        first_started,release_first=asyncio.Event(),asyncio.Event()
        order=[]
        async def collect(**kwargs):
            order.append("poll")
            if len(order)==1:
                first_started.set();await release_first.wait()
            return []
        async def fetch(mid,**kwargs):
            order.append("refresh");return fixture()
        self.runtime._collect=collect
        client=SimpleNamespace(fetch_match=AsyncMock(side_effect=fetch))
        with patch("services.collector.leyu_runtime.LeyuClient.from_config",return_value=client):
            current=asyncio.create_task(self.runtime.collect())
            await first_started.wait()
            precise=asyncio.create_task(self.runtime.refresh_match("m1"))
            await asyncio.sleep(0)
            following=asyncio.create_task(self.runtime.collect())
            await asyncio.sleep(0)
            self.assertFalse(precise.done())
            self.assertFalse(current.cancelled())
            release_first.set()
            await asyncio.gather(current,precise,following)
        self.assertEqual(["poll","refresh","poll"],order)

    async def test_rate_limit_set_by_current_poll_blocks_queued_refresh(self):
        await self.runtime._collection_lock.acquire()
        waiting=asyncio.create_task(self.runtime.refresh_match("m1"))
        await asyncio.sleep(0)
        self.runtime.request_budget.defer_requests(60)
        self.runtime._collection_lock.release()
        with self.assertRaises(RateLimited): await waiting
        self.runtime.load.assert_not_called()

    async def test_controller_deadline_while_waiting_returns_busy_without_cooldown_or_background(self):
        upload=AsyncMock(return_value=1)
        controller=LeyuRefreshController(self.runtime,upload,deadline_seconds=.01)
        await self.runtime._collection_lock.acquire()
        try:
            result=await controller.refresh({"fixture_id":"leyu:m1","source_match_id":"m1"})
            self.assertEqual("refresh_busy",result["reason"])
            self.assertIs(False,result["accepted"])
            self.assertTrue(self.runtime._collection_lock.locked())
            self.assertEqual(0,controller.cooldown_remaining())
            self.assertEqual({},controller._inflight)
            self.runtime.load.assert_not_called()
            upload.assert_not_called()
        finally:
            self.runtime._collection_lock.release()
            await controller.close()

    async def test_auth_expired_refresh_does_not_renew_or_retry(self):
        client=SimpleNamespace(fetch_match=AsyncMock(side_effect=AuthExpired("session_expired")))
        with patch("services.collector.leyu_runtime.LeyuClient.from_config",return_value=client), \
                patch("services.collector.leyu_runtime.AppSessionRenewer") as renewal:
            with self.assertRaises(AuthExpired):
                await self.runtime.refresh_match("m1")
            renewal.assert_not_called()
        client.fetch_match.assert_awaited_once()

    async def test_shared_cooldown_blocks_poll_and_refresh_before_load(self):
        self.runtime.request_budget.defer_requests(30)
        for operation in (self.runtime.collect,lambda:self.runtime.refresh_match("m1")):
            with self.assertRaises(RateLimited):
                await operation()
        self.runtime.load.assert_not_called()

    async def test_complete_empty_detail_removes_markets_and_preserves_other_fixtures(self):
        self.runtime.fixtures["leyu:m1"]["markets"]=[{"market_id":"4","selection":"home"}]
        client=SimpleNamespace(fetch_match=AsyncMock(return_value=fixture()))
        with patch("services.collector.leyu_runtime.LeyuClient.from_config",return_value=client):
            result=await self.runtime.refresh_match("m1")
        self.assertEqual([],result["markets"])
        self.assertTrue(result["markets_complete"])
        self.assertIn("leyu:m2",self.runtime.fixtures)

    async def test_partial_poll_limit_enters_cooldown_before_upload_stage(self):
        client=SimpleNamespace(fetch=AsyncMock(return_value=[]),last_report={"rate_limited":True,"retry_after_seconds":80})
        with patch("services.collector.leyu_runtime.LeyuClient.from_config",return_value=client):
            await self.runtime.collect()
        self.assertGreater(self.runtime.request_budget.cooldown_remaining(),79)

    async def test_actual_provider_start_spacing_survives_slow_configuration_read(self):
        clock=[0.0]
        self.runtime.request_budget=LeyuRequestBudget(clock=lambda:clock[0])
        async def load(): clock[0]+=1
        self.runtime.load=AsyncMock(side_effect=load)
        client=SimpleNamespace(fetch_match=AsyncMock(return_value=fixture()))
        with patch("services.collector.leyu_runtime.LeyuClient.from_config",return_value=client):
            await self.runtime.refresh_match("m1")
            with self.assertRaises(LeyuError) as raised: await self.runtime.refresh_match("m1")
            self.assertEqual("refresh_throttled",raised.exception.category)
            await self.runtime.refresh_match("m1")
        self.assertEqual(2,client.fetch_match.await_count)

    async def test_no_upstream_when_match_missing_finished_or_source_identity_ambiguous(self):
        self.runtime.fixtures["leyu:m1"]["phase"]="finished"
        with self.assertRaises(LeyuError): await self.runtime.refresh_match("m1")
        with self.assertRaises(LeyuError): await self.runtime.refresh_match("unknown")
        self.runtime.fixtures["other"]={**fixture(),"id":"other"}
        with self.assertRaises(LeyuError): await self.runtime.refresh_match("m1")
        self.runtime.load.assert_not_called()


class RefreshControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now=[100.0]
        self.clock=lambda:self.now[0]
        self.runtime=SimpleNamespace(fixtures={"leyu:m1":fixture(),"leyu:m2":fixture("m2")},
            refresh_match=AsyncMock(return_value=fixture()),request_budget=LeyuRequestBudget(clock=self.clock))
        self.upload=AsyncMock(return_value=1)
        self.controller=LeyuRefreshController(self.runtime,self.upload,clock=self.clock)
        self.body={"fixture_id":"leyu:m1","source_match_id":"m1"}

    async def asyncTearDown(self):
        await self.controller.close()

    async def test_success_upload_receipt_is_bool_and_singleflight_upload_once(self):
        entered,release=asyncio.Event(),asyncio.Event()
        async def fetch(mid):
            entered.set();await release.wait();return fixture()
        self.runtime.refresh_match.side_effect=fetch
        tasks=[asyncio.create_task(self.controller.refresh(self.body)) for _ in range(3)]
        await entered.wait();release.set()
        results=await asyncio.gather(*tasks)
        self.assertTrue(all(item["accepted"] is True for item in results))
        self.assertEqual({1},{item["written_fixtures"] for item in results})
        self.runtime.refresh_match.assert_awaited_once_with("m1")
        self.upload.assert_awaited_once()
        self.assertEqual({},self.controller._inflight)

    async def test_database_source_id_resolves_only_bound_known_match(self):
        result=await self.controller.refresh({**self.body,"source_match_id":"leyu:m1"})
        self.assertIs(True,result["accepted"])
        self.assertEqual("leyu:m1",result["source_match_id"])
        self.runtime.refresh_match.assert_awaited_once_with("m1")
        for source in ("leyu:m2","m2","https://evil.invalid"):
            with self.assertRaises(ValueError):
                await self.controller.refresh({**self.body,"source_match_id":source})

    async def test_global_and_per_match_start_spacing_are_at_least_two_seconds(self):
        await self.controller.refresh(self.body)
        self.now[0]+=1.99
        result=await self.controller.refresh({"fixture_id":"leyu:m2","source_match_id":"m2"})
        self.assertEqual("refresh_throttled",result["reason"])
        self.now[0]+=.01
        self.runtime.refresh_match.return_value=fixture("m2")
        self.assertIs(True,(await self.controller.refresh({"fixture_id":"leyu:m2","source_match_id":"m2"}))["accepted"])
        self.assertEqual(2,self.runtime.refresh_match.await_count)

    async def test_rate_limit_defers_shared_poll_and_precise_requests(self):
        self.runtime.refresh_match.side_effect=RateLimited("http_rate_limited",retry_after_seconds=75)
        result=await self.controller.refresh(self.body)
        self.assertEqual("upstream_rate_limited",result["reason"])
        self.assertIs(False,result["accepted"])
        self.assertEqual(75,self.runtime.request_budget.cooldown_remaining())
        self.assertEqual("upstream_cooldown",(await self.controller.refresh(self.body))["reason"])
        with self.assertRaises(RateLimited): self.runtime.request_budget.require_available()
        self.runtime.refresh_match.assert_awaited_once()
        self.upload.assert_not_called()

    async def test_timeout_cancels_work_without_upload_or_retry(self):
        cancelled=asyncio.Event()
        async def fetch(mid):
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        self.runtime.refresh_match.side_effect=fetch
        self.controller.deadline_seconds=.01
        result=await self.controller.refresh(self.body)
        self.assertEqual("refresh_timeout",result["reason"])
        self.assertTrue(cancelled.is_set())
        self.runtime.refresh_match.assert_awaited_once()
        self.upload.assert_not_called()

    async def test_one_cancelled_waiter_does_not_cancel_shared_request(self):
        entered,release=asyncio.Event(),asyncio.Event()
        async def fetch(mid):
            entered.set();await release.wait();return fixture()
        self.runtime.refresh_match.side_effect=fetch
        first=asyncio.create_task(self.controller.refresh(self.body))
        await entered.wait()
        second=asyncio.create_task(self.controller.refresh(self.body))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError): await first
        release.set()
        self.assertIs(True,(await second)["accepted"])
        self.runtime.refresh_match.assert_awaited_once()

    async def test_last_cancelled_waiter_cancels_request_and_releases_entry(self):
        entered,cancelled=asyncio.Event(),asyncio.Event()
        async def fetch(mid):
            entered.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        self.runtime.refresh_match.side_effect=fetch
        task=asyncio.create_task(self.controller.refresh(self.body))
        await entered.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(cancelled.is_set())
        self.assertEqual({},self.controller._inflight)
        self.upload.assert_not_called()

    async def test_busy_other_match_and_finished_match_do_not_request(self):
        entered,release=asyncio.Event(),asyncio.Event()
        async def fetch(mid): entered.set();await release.wait();return fixture()
        self.runtime.refresh_match.side_effect=fetch
        task=asyncio.create_task(self.controller.refresh(self.body))
        await entered.wait()
        other={"fixture_id":"leyu:m2","source_match_id":"m2"}
        self.assertEqual("refresh_busy",(await self.controller.refresh(other))["reason"])
        self.runtime.fixtures["leyu:m2"]["phase"]="finished"
        self.assertEqual("fixture_not_open",(await self.controller.refresh(other))["reason"])
        release.set();await task
        self.runtime.refresh_match.assert_awaited_once()

    async def test_invalid_or_unconfirmed_receipt_never_claims_written(self):
        for receipt in (True,2,-1,None):
            self.now[0]+=2
            self.upload.return_value=receipt
            result=await self.controller.refresh(self.body)
            self.assertIs(False,result["accepted"])
            self.assertEqual("invalid_ingestion_receipt",result["reason"])

    async def test_committed_unchanged_parent_receipt_requires_independent_quote_reread(self):
        self.upload.return_value = 0
        result = await self.controller.refresh(self.body)
        self.assertIs(False, result['accepted'])
        self.assertIs(False, result['written'])
        self.assertEqual(0, result['written_fixtures'])
        self.assertIs(True, result['snapshot_committed'])
        self.assertIs(False, result['quote_writes_verified'])
        self.assertEqual('parent_unchanged_recheck_quotes', result['reason'])
        self.assertEqual(0, self.controller.cooldown_remaining())
        self.upload.assert_awaited_once()

    async def test_untrusted_body_and_diagnostics_never_return_secrets(self):
        with self.assertRaises(ValueError): await self.controller.refresh({**self.body,"url":"private"})
        self.runtime.refresh_match.side_effect=RuntimeError("password private-token")
        result=await self.controller.refresh(self.body)
        self.assertEqual("refresh_failed",result["reason"])
        self.assertNotIn("private",json.dumps(result))


class ControlEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_internal_token_authenticates_refresh_and_old_routes_still_dispatch(self):
        collector=SimpleNamespace(leyu_refresh=AsyncMock(return_value={"accepted":True,"reason":"written"}),
                                  control=AsyncMock(return_value={"state":"ok"}))
        server=ControlServer(collector,"isolated-only",host="127.0.0.1",port=0)
        server.start()
        url="http://127.0.0.1:"+str(server.server.server_port)
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                self.assertEqual(401,(await client.post(url+"/leyu/refresh",json={})).status_code)
                headers={"Authorization":"Bearer isolated-only"}
                result=await client.post(url+"/leyu/refresh",headers=headers,json={"fixture_id":"leyu:m1","source_match_id":"m1"})
                self.assertIs(True,result.json()["accepted"])
                self.assertEqual("no-store",result.headers["cache-control"])
                self.assertEqual(404,(await client.post(url+"/arbitrary",headers=headers,json={})).status_code)
                self.assertEqual("ok",(await client.post(url+"/status",headers=headers,json={})).json()["state"])
            collector.leyu_refresh.assert_awaited_once()
            collector.control.assert_awaited_once_with("/status",{})
        finally:
            await server.close()

    async def test_invalid_refresh_body_is_400_and_missing_callback_is_explicit(self):
        async def reject(body): raise ValueError("invalid body")
        collector=SimpleNamespace(leyu_refresh=reject,control=AsyncMock())
        server=ControlServer(collector,"isolated-only",host="127.0.0.1",port=0)
        server.start()
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                response=await client.post("http://127.0.0.1:"+str(server.server.server_port)+"/leyu/refresh",
                                           headers={"Authorization":"Bearer isolated-only"},json={})
            self.assertEqual(400,response.status_code)
            del collector.leyu_refresh
            self.assertIs(False,(await server.dispatch("/leyu/refresh",{}))["accepted"])
        finally: await server.close()


class WorkerSharedBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={"accepted":0})))
        self.service=CollectorService(self.api,"http://api.invalid/internal","isolated",interval=20,
                                      telegram_factory=FakeTelegram,realtime_factory=FakeRealtime)

    async def asyncTearDown(self):
        await self.service.leyu_refresh.close()
        await self.api.aclose()

    async def run_once(self):
        stop=asyncio.Event()
        waits=[]
        async def wait(event,delay,wake=None):
            waits.append((delay,wake))
            if len(waits)>1 or self.service.leyu_refresh.cooldown_remaining(): event.set()
        with patch("services.collector.worker.wait_for_work",side_effect=wait):
            await self.service.leyu_loop(stop)
        return waits

    async def test_normal_poll_interval_does_not_block_precise_refresh(self):
        self.service.runtime.collect=AsyncMock(return_value=[])
        self.service.runtime.last_report={"complete_market_fixtures":0,"odds_requested_fixtures":0}
        await self.run_once()
        self.assertEqual(0,self.service.leyu_refresh.cooldown_remaining())
        self.assertIs(self.service.telegram.leyu_refresh.__self__,self.service.leyu_refresh)

    async def test_actual_source_failure_and_poll_limit_share_backoff(self):
        self.service.runtime.collect=AsyncMock(side_effect=LeyuError("upstream_timeout"))
        await self.run_once()
        self.assertGreater(self.service.runtime.request_budget.cooldown_remaining(),1)
        self.service.runtime.collect.assert_awaited_once()

    async def test_current_poll_status_is_visible_during_new_poll_and_completion(self):
        started,release=asyncio.Event(),asyncio.Event()
        stop=asyncio.Event()
        self.service.status["leyu"].update(last_poll_at="2026-10-20T00:00:00+00:00",last_poll_elapsed_ms=123)
        async def collect(**kwargs):
            started.set();await release.wait();stop.set();return []
        self.service.runtime.collect=AsyncMock(side_effect=collect)
        self.service.runtime.last_report={"complete_market_fixtures":0,"odds_requested_fixtures":0}
        async def wait(*args,**kwargs): pass
        with patch("services.collector.worker.wait_for_work",wait):
            task=asyncio.create_task(self.service.leyu_loop(stop))
            await started.wait()
            status=self.service.status["leyu"]
            self.assertIs(True,status["poll_in_progress"])
            self.assertIsNone(status["current_poll_completed_at"])
            self.assertNotEqual(status["last_poll_at"],status["current_poll_started_at"])
            release.set();await task
        self.assertIs(False,status["poll_in_progress"])
        self.assertIsNotNone(status["current_poll_completed_at"])

    async def test_ingestion_error_does_not_rate_limit_upstream_refresh(self):
        self.service.runtime.collect=AsyncMock(return_value=[])
        self.service.upload=AsyncMock(side_effect=LeyuError("invalid_ingestion_receipt"))
        await self.run_once()
        self.assertEqual(0,self.service.leyu_refresh.cooldown_remaining())

    async def test_precise_rate_cooldown_prevents_poll_call_and_ws_cannot_wake(self):
        self.service.runtime.collect=AsyncMock(return_value=[])
        self.service.leyu_refresh.defer_requests(75)
        self.service.resync.set()
        waits=await self.run_once()
        self.service.runtime.collect.assert_not_called()
        self.assertGreater(waits[0][0],74)
        self.assertIsNone(waits[0][1])
