import asyncio
import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import gzip
import json
import unittest
from unittest.mock import AsyncMock, patch
import uuid

import httpx

from services.collector.leyu_protocol import AuthExpired, LeyuError
from services.executor.account_protocol import (
    AccountProtocolClient, NativeSelection, SubmissionPermit, SubmissionRejected, SubmissionUnknown,
    AMOUNT_PATH, ORDERS_PATH, LATEST_MARKET_PATH, LIMITS_PATH, BET_PATH, normalize_order, order_page,
)
from services.executor.account_session import AccountScope, AccountSessionManager, RenewalHooks, VenueSession


def scope(revision=1):
    return AccountScope(str(uuid.uuid4()), str(uuid.uuid4()), revision)


def selection(**changes):
    values = {"match_id": "match", "market_instance_id": "market", "play_id": "2", "selection_id": "option",
              "selection_code": "Over", "sport_id": "1", "chpid": "2", "kind": "total", "period": "full_time",
              "line": Decimal("2.5"), "price": Decimal("1.95"), "phase": "live", "score_benchmark": "1:0"}
    return NativeSelection(**{**values, **changes})


def latest():
    return [{"id": "market", "matchInfoId": "match", "playId": "2", "matchStatus": 1,
             "matchOver": 0, "matchHandicapStatus": 0, "status": 0, "marketValue": "2.5",
             "marketOddsList": [{"id": "option", "oddsStatus": 1, "oddsType": "Over", "oddsValue": "195000"}]}]


def limits():
    return [{"code": 0, "playOptionsId": "option", "playId": "", "type": "", "minBet": "1", "orderMaxPay": "10"}]


class AccountSessionTests(unittest.IsolatedAsyncioTestCase):
    def test_scope_mismatch_and_invalid_scope_rejected(self):
        owner, other = scope(), scope()
        session = VenueSession(owner, "https://account.invalid", "private-session", "https://ui.invalid")
        with self.assertRaises(LeyuError):
            AccountProtocolClient(other, session)
        with self.assertRaises(LeyuError):
            AccountScope("not-user", "not-account", 1)
        with self.assertRaises(LeyuError):
            replace(owner, revision=0)
        with self.assertRaises(LeyuError):
            VenueSession(owner, "http://unsafe.invalid", "session", "https://ui.invalid")
        self.assertNotIn("private-session", repr(session))

    def test_account_config_cannot_accept_collector_token_alias(self):
        hooks = RenewalHooks(AsyncMock(), AsyncMock(), AsyncMock())
        with self.assertRaises(LeyuError):
            AccountSessionManager(scope(), {"leyu_api_token": "collector-app-token"}, hooks)
        with patch.dict("os.environ", {"LEYU_APP_TOKEN": "unrelated-collector-token", "LEYU_REQUEST_ID": "unrelated-session"}):
            manager = AccountSessionManager(scope(), {}, hooks)
            with self.assertRaises(LeyuError):
                manager.venue_session()

    async def test_renewal_callbacks_always_scoped_and_revision_advances(self):
        owner = scope()
        calls = []

        async def claim(current, identity_hash):
            calls.append(("claim", current, identity_hash))
            return "attempt"

        async def persist(current, expected, updates):
            calls.append(("persist", current, expected, dict(updates)))
            return expected + 1

        async def finish(*args):
            calls.append(("finish", *args))

        def factory(config, **callbacks):
            self.assertEqual("own-private-token", config["leyu_app_token"])

            async def renew(reason):
                self.assertEqual("session_expired", reason)
                attempt = await callbacks["claim"]("h" * 64)
                await callbacks["persist"]({"leyu_app_token": "own-new-token"})
                await callbacks["persist"]({"leyu_api_base": "https://own.invalid", "leyu_origin": "https://own-ui.invalid", "leyu_request_id": "own-session"})
                await callbacks["finish"](attempt, "success", {"login_submitted": True})

            return type("Renewer", (), {"renew": staticmethod(renew)})()

        manager = AccountSessionManager(owner, {"leyu_app_token": "own-private-token"}, RenewalHooks(claim, persist, finish))
        with patch("services.executor.account_session.AppSessionRenewer", side_effect=factory):
            session = await manager.renew("session_expired")
        self.assertEqual((owner.user_id, owner.account_id, 3), (session.scope.user_id, session.scope.account_id, session.scope.revision))
        self.assertEqual("own-session", session.request_id)
        self.assertEqual([1, 2], [call[2] for call in calls if call[0] == "persist"])
        self.assertTrue(all(call[1].user_id == owner.user_id and call[1].account_id == owner.account_id for call in calls))


class AccountHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.owner = scope()
        self.session = VenueSession(self.owner, "https://account.invalid", "own-business-session", "https://own-ui.invalid")
        self.responses = {AMOUNT_PATH: {"amount": "111.86"}, LATEST_MARKET_PATH: latest(), LIMITS_PATH: limits(),
                          BET_PATH: {"code": "0", "data": {"orderDetailRespList": [{"orderNo": "ORDER-1", "orderStatusCode": 1}]}},
                          ORDERS_PATH: {"records": [], "total": 0}}
        self.requests = []
        self.active = self.peak = 0
        self.transport = httpx.MockTransport(self.handler)
        self.client = AccountProtocolClient(self.owner, self.session, transport=self.transport)
        self.begin = datetime(2026, 10, 20, tzinfo=timezone.utc)
        self.end = self.begin + timedelta(days=1)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def handler(self, request):
        self.requests.append(request)
        self.assertEqual("own-business-session", request.headers["requestId"])
        self.assertEqual("https://own-ui.invalid", request.headers["origin"])
        self.assertNotIn("authorization", request.headers)
        self.assertNotIn("x-api-token", request.headers)
        value = self.responses[request.url.path]
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.001)
        self.active -= 1
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value(request)
        if isinstance(value, httpx.Response):
            return value
        if request.url.path == BET_PATH or request.url.path == AMOUNT_PATH:
            return httpx.Response(200, json=value)
        return httpx.Response(200, json={"code": "0000000", "data": value})

    async def prepare(self):
        return await self.client.preflight(selection(), Decimal("2"), device_imei="user-device")

    def permit(self, prepared, **changes):
        return SubmissionPermit(**{"scope": self.owner, "execution_id": str(uuid.uuid4()), "prepared_digest": prepared.digest, **changes})

    async def test_balance_plain_and_wrapped_zero_distinguished_from_missing(self):
        self.assertEqual("111.86", (await self.client.balance())["available"])
        self.responses[AMOUNT_PATH] = {"code": "0", "data": {"amount": "0"}}
        self.assertEqual("0", (await self.client.balance())["available"])
        for invalid in ({}, {"amount": "NaN"}, {"amount": -1}, {"amount": "2", "balance": "3"}):
            self.responses[AMOUNT_PATH] = invalid
            with self.assertRaises(LeyuError):
                await self.client.balance()

    async def test_preflight_three_reads_concurrent_exact_native_request(self):
        prepared = await self.prepare()
        self.assertEqual(3, self.peak)
        self.assertNotIn(BET_PATH, [request.url.path for request in self.requests])
        detail = prepared.payload["seriesOrders"][0]["orderDetailList"][0]
        self.assertEqual(("2.00", "1.95", "195000", "2.5", "1:0"),
                         (detail["betAmount"], detail["oddFinally"], detail["odds"], detail["marketValue"], detail["scoreBenchmark"]))
        self.assertEqual(2, prepared.payload["acceptOdds"])
        latest_body = json.loads(next(request.content for request in self.requests if request.url.path == LATEST_MARKET_PATH))
        self.assertEqual("option", latest_body["idList"][0]["oddsId"])
        self.assertEqual(2, latest_body["idList"][0]["matchType"])
        altered = prepared.payload
        altered["seriesOrders"].clear()
        self.assertEqual(1, len(prepared.payload["seriesOrders"]))

    async def test_changed_native_identity_quote_state_direction_and_limits_rejected(self):
        cases = [(LATEST_MARKET_PATH, "id", "other"), (LATEST_MARKET_PATH, "matchInfoId", "other"),
                 (LATEST_MARKET_PATH, "playId", "other"), (LATEST_MARKET_PATH, "matchStatus", 0),
                 (LATEST_MARKET_PATH, "matchHandicapStatus", 1), (LATEST_MARKET_PATH, "marketValue", "3.0"),
                 (LIMITS_PATH, "playOptionsId", "other"), (LIMITS_PATH, "playId", "other"),
                 (LIMITS_PATH, "code", "0401015"), (LIMITS_PATH, "minBet", "NaN"),
                 (LIMITS_PATH, "orderMaxPay", "1")]
        for path, key, value in cases:
            self.responses[LATEST_MARKET_PATH], self.responses[LIMITS_PATH] = latest(), limits()
            self.responses[path][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(LeyuError):
                await self.prepare()
        for key, value in (("id", "other"), ("oddsType", "Under"), ("oddsStatus", 3), ("oddsValue", "195001")):
            self.responses[LATEST_MARKET_PATH], self.responses[LIMITS_PATH] = latest(), limits()
            self.responses[LATEST_MARKET_PATH][0]["marketOddsList"][0][key] = value
            with self.subTest(option=key), self.assertRaises(LeyuError):
                await self.prepare()
        self.assertNotIn(BET_PATH, [request.url.path for request in self.requests])

    async def test_prematch_and_invalid_stake_never_guess_or_request(self):
        for stage in ("scheduled", "finished", "unknown"):
            with self.assertRaisesRegex(LeyuError, "prematch_execution_protocol_unverified"):
                await self.client.preflight(selection(phase=stage), Decimal("2"))
        for stake in (Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("1.001")):
            with self.assertRaises(LeyuError):
                await self.client.preflight(selection(), stake)
        self.assertEqual([], self.requests)

    async def test_live_write_disabled_by_default(self):
        prepared = await self.prepare()
        with self.assertRaisesRegex(LeyuError, "live_orders_disabled"):
            await self.client.submit_once(prepared, self.permit(prepared))
        self.assertNotIn(BET_PATH, [request.url.path for request in self.requests])

    async def test_scoped_permit_one_send_only_and_pending_receipt(self):
        self.client.live_orders_enabled = True
        prepared = await self.prepare()
        permit = self.permit(prepared)
        result = await self.client.submit_once(prepared, permit)
        self.assertEqual(("accepted", "ORDER-1"), (result["state"], result["order_no"]))
        with self.assertRaises(LeyuError):
            await self.client.submit_once(prepared, permit)
        with self.assertRaises(LeyuError):
            await self.client.submit_once(prepared, self.permit(prepared))
        self.assertEqual(1, sum(request.url.path == BET_PATH for request in self.requests))
        self.responses[BET_PATH]["data"]["orderDetailRespList"][0]["orderStatusCode"] = 2
        fresh = await self.prepare()
        self.assertEqual("pending", (await self.client.submit_once(fresh, self.permit(fresh)))["state"])

    async def test_cross_scope_tampered_expired_and_unissued_prepared_never_send(self):
        self.client.live_orders_enabled = True
        prepared = await self.prepare()
        wrong = self.permit(prepared, scope=scope())
        with self.assertRaises(LeyuError):
            await self.client.submit_once(prepared, wrong)
        for modified in (replace(prepared, payload_json="{}"), replace(prepared, expires_monotonic=prepared.expires_monotonic + 1000),
                         replace(prepared, scope=replace(self.owner, revision=2))):
            with self.assertRaises(LeyuError):
                await self.client.submit_once(modified, self.permit(prepared))
        with patch("services.executor.account_protocol.time.monotonic", return_value=prepared.expires_monotonic + 1):
            with self.assertRaises(LeyuError):
                await self.client.submit_once(prepared, self.permit(prepared))
        self.assertNotIn(BET_PATH, [request.url.path for request in self.requests])

    async def test_unknown_submission_is_single_send_and_does_not_leak(self):
        self.client.live_orders_enabled = True
        self.responses[BET_PATH] = httpx.ReadTimeout("private-network-credential")
        prepared = await self.prepare()
        permit = self.permit(prepared)
        with self.assertRaises(SubmissionUnknown) as raised:
            await self.client.submit_once(prepared, permit)
        self.assertNotIn("private-network-credential", str(raised.exception))
        with self.assertRaises(LeyuError):
            await self.client.submit_once(prepared, permit)
        self.assertEqual(1, sum(request.url.path == BET_PATH for request in self.requests))

    async def test_explicit_rejection_vs_unknown_receipt_not_replayed(self):
        self.client.live_orders_enabled = True
        cases = [({"code": "0401013"}, SubmissionRejected),
                 ({"code": 0, "data": {"orderDetailRespList": [{"orderStatusCode": 0}]}}, SubmissionRejected),
                 ({"code": 0, "data": {}}, SubmissionUnknown),
                 ({"code": 0, "data": {"orderDetailRespList": [{"orderStatusCode": 1}]}}, SubmissionUnknown),
                 ({"code": 0, "data": {"orderDetailRespList": [{"orderStatusCode": 9, "orderNo": "O"}]}}, SubmissionUnknown),
                 ({"code": None}, SubmissionUnknown), ({"code": True}, SubmissionUnknown),
                 (httpx.Response(403, text="private-refusal-body"), SubmissionUnknown),
                 (httpx.Response(200, text="private-invalid-json"), SubmissionUnknown)]
        for response, error in cases:
            self.responses[BET_PATH] = response
            prepared = await self.prepare()
            before = sum(request.url.path == BET_PATH for request in self.requests)
            with self.subTest(error=error, response_type=type(response).__name__), self.assertRaises(error) as raised:
                await self.client.submit_once(prepared, self.permit(prepared))
            self.assertNotIn("private-", str(raised.exception))
            self.assertEqual(before + 1, sum(request.url.path == BET_PATH for request in self.requests))

    async def test_read_auth_error_not_retried_as_password_or_write(self):
        self.responses[AMOUNT_PATH] = {"code": "0401013"}
        with self.assertRaises(AuthExpired):
            await self.client.balance()
        self.assertEqual(1, len(self.requests))

    async def test_compressed_order_page_has_explicit_window_and_no_fake_pnl(self):
        data = {"total": 1, "records": [{"day": "2026-10-20", "data": [
            {"orderNo": "O1", "betAmount": "2.00", "backAmount": "3.90", "settleTime": "1792468800000",
             "detailList": [{"matchId": "match", "playNameCn": "全场大小", "playOptionNameCn": "大2.5"}]}]}]}
        encoded = base64.b64encode(gzip.compress(json.dumps(data).encode())).decode()
        self.responses[ORDERS_PATH] = encoded
        page = await self.client.orders("settled", begin=self.begin, end=self.end)
        self.assertEqual((1, "1.90", "gross_return_minus_stake"), (page["total"], page["items"][0]["net_profit"], page["items"][0]["pnl_basis"]))
        body = json.loads(self.requests[-1].content)
        self.assertEqual(int(self.begin.timestamp() * 1000), body["beginTime"])
        self.assertEqual(int(self.end.timestamp() * 1000), body["endTime"])
        self.assertEqual(1, body["orderStatus"])
        self.assertIsNone(page["items"][0]["fees"])

    async def test_lookup_paginates_past_twenty_records_without_absence_guarantee(self):
        def pages(request):
            body = json.loads(request.content)
            if body["orderStatus"] == 0:
                return {"total": 0, "records": []}
            page = body["page"]
            return {"total": 205, "records": [{"orderNo": "TARGET" if n == 204 else f"O{n}", "betAmount": "2"}
                     for n in range((page - 1) * 100, min(page * 100, 205))]}

        self.responses[ORDERS_PATH] = pages
        result = await self.client.lookup("TARGET", begin=self.begin, end=self.end, max_pages=3)
        self.assertTrue(result["found"])
        self.assertTrue(result["scan_complete"])
        self.assertFalse(result["absence_proves_rejection"])
        self.assertEqual(4, len(self.requests))
        limited = await self.client.lookup("TARGET", begin=self.begin, end=self.end, max_pages=2)
        self.assertFalse(limited["found"])
        self.assertFalse(limited["scan_complete"])

    async def test_lookup_missing_total_duplicate_pages_and_detail_ids_remain_unresolved(self):
        self.responses[ORDERS_PATH] = {"records": [{"betNo": "TARGET", "betAmount": "2"}]}
        result = await self.client.lookup("TARGET", begin=self.begin, end=self.end)
        self.assertFalse(result["found"])
        self.assertFalse(result["scan_complete"])
        self.responses[ORDERS_PATH] = {"total": 1000, "records": [{"orderNo": "other"}]}
        result = await self.client.lookup("TARGET", begin=self.begin, end=self.end, max_pages=2)
        self.assertFalse(result["scan_complete"])
        self.assertFalse(result["absence_proves_rejection"])

    async def test_conflicting_target_receipts_never_selected_as_authoritative(self):
        self.responses[ORDERS_PATH] = {"total": 2, "records": [
            {"orderNo": "TARGET", "profitAmount": "1"}, {"orderNo": "TARGET", "profitAmount": "-1"}]}
        result = await self.client.lookup("TARGET", begin=self.begin, end=self.end)
        self.assertTrue(result["ambiguous"])
        self.assertFalse(result["found"])
        self.assertIsNone(result["receipt"])
        self.assertFalse(result["scan_complete"])


class OrderNormalizationTests(unittest.TestCase):
    def test_invalid_and_unknown_profit_never_become_zero(self):
        for row in ({"orderNo": "O", "winAmount": "3", "betAmount": "2"}, {"orderNo": "O", "profitAmount": "NaN", "betAmount": "2", "backAmount": "4"}):
            self.assertIsNone(normalize_order(row, "settled")["net_profit"])
        self.assertEqual("0", normalize_order({"orderNo": "O", "profitAmount": 0}, "settled")["net_profit"])
        self.assertIsNone(normalize_order({"orderNo": "O"}, "settled")["settled_at"])

    def test_duplicate_missing_and_malformed_records_are_explicit(self):
        result = order_page({"total": 4, "records": [{"orderNo": "O"}, {"orderNo": "O"}, {}, "invalid"]}, "settled", 1, 100)
        self.assertEqual(1, len(result["items"]))
        self.assertEqual(3, result["malformed_records"])
        self.assertFalse(result["absence_proves_rejection"])

    def test_invalid_native_price_and_direction_rejected(self):
        for changes in ({"price": Decimal("NaN")}, {"price": Decimal("1.950001")}, {"selection_code": "1"}, {"selection_id": ""}):
            with self.assertRaises(LeyuError):
                selection(**changes)


if __name__ == "__main__":
    unittest.main()
