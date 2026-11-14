"""Basketball account requests use native identities; no submission is made."""
from dataclasses import replace
from decimal import Decimal
import json
import unittest
import uuid

import httpx

from services.collector.leyu_protocol import LeyuError
from services.execution_scope import (
    execution_market_supported, native_execution_market_supported, native_sport_id,
)
from services.executor.account_protocol import (
    AccountProtocolClient, NativeSelection, AMOUNT_PATH, LATEST_MARKET_PATH, LIMITS_PATH,
)
from services.executor.account_session import AccountScope, VenueSession


def basketball_selection(market="38", **changes):
    kind, code, line = {
        "37": ("winner", "1", None),
        "38": ("total", "Over", Decimal("179.5")),
        "39": ("handicap", "1", Decimal("-6.5")),
    }[market]
    return NativeSelection(**{
        "match_id": "5817686", "market_instance_id": "native-market", "play_id": market,
        "selection_id": "native-option", "selection_code": code, "sport_id": "2",
        "chpid": market, "kind": kind, "period": "full_time", "line": line,
        "price": Decimal("1.95"), "phase": "live", "score_benchmark": "66:59", **changes,
    })


class BasketballExecutionScopeTests(unittest.TestCase):
    def test_native_sport_and_verified_market_scope(self):
        self.assertEqual("1", native_sport_id("football"))
        self.assertEqual("2", native_sport_id("basketball"))
        self.assertIsNone(native_sport_id("tennis"))
        for market, kind in (("37", "winner"), ("38", "total"), ("39", "handicap")):
            self.assertTrue(execution_market_supported("basketball", market, kind, "full_time"))
            self.assertTrue(native_execution_market_supported("2", market, kind, "full_time"))
            for period in ("first_half", "second_half", "quarter4", "unknown"):
                self.assertFalse(execution_market_supported("basketball", market, kind, period))
        for sport, market, kind, period in (
            ("basketball", "2", "total", "full_time"),
            ("basketball", "38", "handicap", "full_time"),
            ("basketball", "37", "total", "full_time"),
            ("basketball", "39", "winner", "full_time"),
            ("basketball", "63", "total", "quarter4"),
            ("tennis", "38", "total", "full_time"),
        ):
            self.assertFalse(execution_market_supported(sport, market, kind, period))

    def test_native_selection_rejects_cross_sport_market_period_and_draw(self):
        for changes in (
            {"sport_id": "3"}, {"chpid": "2"}, {"chpid": "39"},
            {"period": "first_half"}, {"period": "second_half"},
        ):
            with self.subTest(changes=changes), self.assertRaises(LeyuError):
                basketball_selection(**changes)
        with self.assertRaises(LeyuError):
            basketball_selection("37", selection_code="X")


class BasketballPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def prepare(self, selected, *, raw_line=None, current_price="203000", closed=False):
        owner = AccountScope(str(uuid.uuid4()), str(uuid.uuid4()), 1)
        session = VenueSession(owner, "https://account.invalid", "isolated", "https://ui.invalid")
        seen = []
        if raw_line is None:
            raw_line = "" if selected.line is None else str(selected.line)

        def handler(request):
            seen.append(request)
            if request.url.path == LATEST_MARKET_PATH:
                value = [{"id": selected.market_instance_id, "matchInfoId": selected.match_id,
                          "playId": selected.play_id, "matchStatus": 0 if selected.phase == "scheduled" else 1,
                          "matchOver": 0, "matchHandicapStatus": 0, "status": 1 if closed else 0,
                          "marketValue": raw_line,
                          "marketOddsList": [{"id": "rotated-option", "oddsType": selected.selection_code,
                                              "oddsStatus": 1, "oddsValue": current_price}]}]
            elif request.url.path == LIMITS_PATH:
                value = [{"code": 0, "playOptionsId": "rotated-option", "playId": selected.play_id,
                          "type": 1, "minBet": "1", "orderMaxPay": "10"}]
            elif request.url.path == AMOUNT_PATH:
                return httpx.Response(200, json={"amount": "100"})
            else:
                raise AssertionError("Basketball verification must never submit an order")
            return httpx.Response(200, json={"code": "0000000", "data": value})

        async with AccountProtocolClient(owner, session, transport=httpx.MockTransport(handler)) as client:
            prepared = await client.preflight(selected, Decimal("2"), device_imei="isolated")
        return prepared, seen

    async def test_live_and_pregame_native_requests_preserve_sport_market_and_current_price(self):
        for phase, match_type in (("live", 2), ("scheduled", 1)):
            for market, codes in (("37", ("1", "2")), ("38", ("Over", "Under")), ("39", ("1", "2"))):
                for code in codes:
                    with self.subTest(phase=phase, market=market, selection=code):
                        selected = basketball_selection(market, phase=phase, selection_code=code,
                                                        score_benchmark="" if phase == "scheduled" else "66:59")
                        prepared, requests = await self.prepare(selected)
                        detail = prepared.payload["seriesOrders"][0]["orderDetailList"][0]
                        self.assertEqual(("2", market, code, match_type, "2.03", "rotated-option"),
                                         tuple(detail[k] for k in ("sportId", "playId", "playOptions", "matchType", "oddFinally", "playOptionsId")))
                        self.assertEqual("" if market == "37" else str(selected.line), detail["marketValue"])
                        latest = json.loads(next(r.content for r in requests if r.url.path == LATEST_MARKET_PATH))["idList"][0]
                        self.assertEqual((2, market, match_type), (latest["sportId"], latest["chpid"], latest["matchType"]))
                        limit = json.loads(next(r.content for r in requests if r.url.path == LIMITS_PATH))["orderMaxBetMoney"][0]
                        self.assertEqual((match_type, "203000", "rotated-option"),
                                         (limit["matchType"], limit["oddsValue"], limit["playOptionId"]))

    async def test_away_handicap_keeps_provider_home_line_and_detects_changed_line(self):
        selected = basketball_selection("39", selection_code="2")
        prepared, _ = await self.prepare(selected)
        detail = prepared.payload["seriesOrders"][0]["orderDetailList"][0]
        self.assertEqual("-6.5", detail["marketValue"])
        self.assertEqual("66:59", detail["scoreBenchmark"])
        for raw_line in ("6.5", "-7.5"):
            with self.subTest(raw_line=raw_line), self.assertRaises(LeyuError) as raised:
                await self.prepare(selected, raw_line=raw_line)
            self.assertEqual("selected_native_quote_changed", raised.exception.category)

    async def test_closed_basketball_market_is_never_prepared(self):
        with self.assertRaises(LeyuError) as raised:
            await self.prepare(basketball_selection(), closed=True)
        self.assertEqual("selected_native_market_not_open", raised.exception.category)
