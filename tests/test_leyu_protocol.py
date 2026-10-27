import asyncio
import base64
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import json
import unittest

import httpx

from services.collector.leyu import (LeyuClient, DEFAULT_FIXTURE_PATH, DEFAULT_ODDS_PATH,
                                     DEFAULT_MATCH_ODDS_PATH)
from services.collector.leyu_protocol import (
    AuthExpired, LeyuError, RateLimited, decode_envelope, decode_payload, parse_fixtures,
    parse_line, parse_quotes, score, timestamp,
)


def match(mid="m1", **changes):
    return {"mid": mid, "csid": "1", "tid": "l1", "mhn": "甲队", "man": "乙队",
            "mgt": "1790784000000", "ms": 1, "mst": "1564", "mmp": "6", **changes}


def market_row(mid="m1", **changes):
    return {**match(mid), "hpsPns": [{"chpid": "4", "hpn": "全场让球", "hpt": 2}],
            "hpsData": [{"hps": [{"chpid": "4", "hpid": "4", "ctsp": "1790785712143",
             "hl": {"hid": "instance1", "hv": "-0.5/-1", "hs": 0, "ol": [
                 {"oid": "option1", "os": 1, "ot": "1", "ov": 211000},
                 {"oid": "option2", "os": 1, "ot": "2", "ov": 178000},
             ]}}]}], **changes}


class EnvelopeTests(unittest.TestCase):
    def test_compressed_envelope_and_plain_json_scalar(self):
        blob = {"matchsList": [match()]}
        encoded = base64.b64encode(gzip.compress(json.dumps(blob).encode())).decode()
        self.assertEqual(blob, decode_envelope({"code": "0000000", "data": encoded}))
        wrapped = "\n".join(encoded[i:i + 76] for i in range(0, len(encoded), 76))
        self.assertEqual(blob, decode_payload(wrapped))
        self.assertEqual(blob, decode_payload(json.dumps(blob)))
        self.assertEqual("1790784000000", decode_payload("1790784000000"))

    def test_broken_gzip_fails_instead_of_becoming_empty_data(self):
        encoded = base64.b64encode(gzip.compress(b"{}")[:-4]).decode()
        with self.assertRaises(LeyuError):
            decode_payload(encoded)

    def test_rate_limit_and_permission_do_not_trigger_login(self):
        with self.assertRaises(RateLimited):
            decode_envelope({"code": "0401038"})
        with self.assertRaises(AuthExpired):
            decode_envelope({"code": "0401013"})
        with self.assertRaises(LeyuError) as raised:
            decode_envelope({"code": "0401015", "msg": "private-token"})
        self.assertNotIsInstance(raised.exception, AuthExpired)
        self.assertNotIn("private-token", str(raised.exception))


class FixtureTests(unittest.TestCase):
    def test_league_sport_score_and_clock_contract(self):
        payload = {"spList": [{"csid": "1", "csna": "足球"}],
                   "tids_obj": [{"tid": "l1", "tn": "授权联赛"}],
                   "matchsList": [match(msc=["S10|9:9", "S2|0:1", "S1|2:1"])]}
        row = parse_fixtures(payload)[0]
        self.assertEqual(("football", "授权联赛", 26), (row["sport"], row["competition_name"], row["clock_minute"]))
        self.assertEqual((2, 1, 0, 1), (row["home_score"], row["away_score"], row["half_home_score"], row["half_away_score"]))
        self.assertIsNone(row["source_updated_at"])
        self.assertEqual("unknown", row["source_time_quality"])
        json.dumps(row)

    def test_basketball_never_silently_becomes_football(self):
        self.assertEqual("basketball", parse_fixtures([match(csid="2")])[0]["sport"])
        self.assertEqual("source_999", parse_fixtures([match(csid="999")])[0]["sport"])

    def test_scheduled_finished_unknown_and_clock_zero_period(self):
        self.assertEqual("scheduled", parse_fixtures([match(ms=0)])[0]["phase"])
        self.assertEqual("finished", parse_fixtures([match(ms=1, mmp="999")])[0]["phase"])
        self.assertEqual("cancelled", parse_fixtures([match(ms=909)])[0]["phase"])
        self.assertIsNone(parse_fixtures([match(mmp="0")])[0]["clock_minute"])
        self.assertEqual(45, parse_fixtures([match(mst="45:00")])[0]["clock_minute"])

    def test_missing_kickoff_is_not_replaced_with_now(self):
        self.assertEqual([], parse_fixtures([match(mgt="invalid")]))
        self.assertIsNone(timestamp("not-a-timestamp"))
        self.assertIsNone(timestamp(datetime(2026, 1, 1)))
        self.assertIsNotNone(timestamp("1790784000000"))

    def test_score_uses_exact_period_key_and_absence(self):
        self.assertEqual((None, None), score("S10|4:3"))
        self.assertEqual((3, 1), score("S1|3:1"))
        self.assertEqual((None, None), score(None))


class QuoteTests(unittest.TestCase):
    def test_split_lines_decimal_and_sign_inheritance(self):
        for raw, expected in [("0/0.5", ".25"), ("-0.5/-1", "-.75"), ("-0.5/1", "-.75"), ("-1/1.5", "-1.25")]:
            self.assertEqual(Decimal(expected), parse_line(raw))
        for raw in ("garbage", "NaN", "Infinity", "3+", "0//.5", ""):
            self.assertIsNone(parse_line(raw))

    def test_dict_hl_and_row_level_definitions(self):
        row = parse_quotes({"data": [market_row()]}, "leyu:m1")[0]
        self.assertEqual(("全场让球", "-0.75", "home", "2.11", "full_time"),
                         (row["market_name"], row["line"], row["selection"], row["price"], row["period"]))
        self.assertEqual(("instance1", "option1", "4"), (row["market_instance_id"], row["selection_id"], row["play_id"]))
        self.assertEqual("active", row["status"])
        self.assertEqual("unknown", row["score_basis"])
        json.dumps(row)

    def test_only_explicit_prematch_proves_full_period_score_basis(self):
        self.assertEqual("full_period", parse_quotes(market_row(ms=0), "leyu:m1")[0]["score_basis"])
        self.assertEqual("unknown", parse_quotes(market_row(ms=1), "leyu:m1")[0]["score_basis"])

    def test_added_lines_do_not_merge_and_scope_filters_match(self):
        row = market_row()
        added = {"chpid": "4", "ctsp": "1790785712143", "hl": [{"hv": "1.5", "hid": "extra", "ol": [{"ot": "1", "ov": 220000, "oid": "e"}]}]}
        row["hpsData"][0]["hpsAdd"] = [added]
        quotes = parse_quotes({"data": [row, market_row("other")]}, "leyu:m1")
        self.assertEqual(3, len(quotes))
        self.assertEqual({"-0.75", "1.5"}, {q["line"] for q in quotes})

    def test_unknown_source_time_stays_unknown_despite_capture_time(self):
        row = market_row()
        row["hpsData"][0]["hps"][0].pop("ctsp")
        quote = parse_quotes(row, "leyu:m1", datetime.now(timezone.utc))[0]
        self.assertIsNone(quote["source_updated_at"])
        self.assertEqual("unknown", quote["source_time_quality"])

    def test_missing_option_status_or_identifiers_cannot_be_active(self):
        for field in ("os", "oid"):
            row = market_row()
            row["hpsData"][0]["hps"][0]["hl"]["ol"][0].pop(field)
            self.assertEqual("suspended", parse_quotes(row, "leyu:m1")[0]["status"])
        row = market_row()
        row["hpsData"][0]["hps"][0]["hl"].pop("hs")
        self.assertEqual("suspended", parse_quotes(row, "leyu:m1")[0]["status"])

    def test_suspended_and_invalid_price_never_active(self):
        row = market_row()
        line = row["hpsData"][0]["hps"][0]["hl"]
        line["ol"][0]["os"] = 2
        line["ol"][1]["ov"] = "NaN"
        quotes = parse_quotes(row, "leyu:m1")
        self.assertEqual(["suspended", "suspended"], [q["status"] for q in quotes])
        self.assertIsNone(quotes[1]["price"])
        line["hs"] = 3
        self.assertEqual("closed", parse_quotes(row, "leyu:m1")[0]["status"])

    def test_invalid_line_not_zero_and_winner_has_explicit_period(self):
        row = market_row()
        row["hpsData"][0]["hps"][0]["hl"]["hv"] = "invalid"
        self.assertEqual([], parse_quotes(row, "leyu:m1"))
        row = {"hps": [{"chpid": "17", "hl": {"ol": [{"ot": "X", "ov": 320000}]}}]}
        quote = parse_quotes(row, "leyu:m1")[0]
        self.assertEqual(("first_half", "draw", "0"), (quote["period"], quote["selection"], quote["line"]))


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def test_missing_request_id_fails_before_network(self):
        with self.assertRaisesRegex(LeyuError, "authorized_session_missing"):
            LeyuClient("https://upstream.invalid")

    def test_config_keeps_authorization_and_cookie(self):
        client = LeyuClient.from_config({"leyu_api_base": "https://upstream.invalid", "leyu_request_id": "real-session",
                                       "leyu_origin": "https://ui.invalid", "leyu_cookie": "route=abc", "leyu_business_cookie_token": "token"})
        self.assertEqual("real-session", client.headers()["requestId"])
        self.assertEqual("https://ui.invalid", client.headers()["origin"])
        self.assertEqual("route=abc; X-API-TOKEN=token", client.headers()["cookie"])
        self.assertEqual("cross-site", client.headers()["sec-fetch-site"])
        self.assertEqual("cors", client.headers()["sec-fetch-mode"])
        self.assertEqual("empty", client.headers()["sec-fetch-dest"])
        self.assertIn("Chrome/153.0.0.0", client.headers()["user-agent"])

    def test_app_token_not_forwarded_to_business_gateway(self):
        client = LeyuClient.from_config({"leyu_api_base": "https://upstream.invalid", "leyu_request_id": "session", "leyu_api_token": "private-app-token"})
        self.assertNotIn("private-app-token", str(client.headers()))

    async def test_full_schedule_batches_beyond_twenty_with_bounded_concurrency(self):
        active = peak = 0
        batch_sizes = []

        async def handler(request):
            nonlocal active, peak
            self.assertEqual("session", request.headers["requestId"])
            if request.url.path == DEFAULT_FIXTURE_PATH:
                return httpx.Response(200, json={"code": "0000000", "data": {"matchsList": [match(str(i)) for i in range(45)]}})
            self.assertEqual(DEFAULT_ODDS_PATH, request.url.path)
            body = json.loads(request.content)
            self.assertIsInstance(body["mids"], str)
            mids = body["mids"].split(",")
            batch_sizes.append(len(mids))
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return httpx.Response(200, json={"code": "0", "data": {"data": [market_row(mid) for mid in mids]}})

        client = LeyuClient("https://upstream.invalid", request_id="session", transport=httpx.MockTransport(handler), batch_interval=0, concurrency=2)
        fixtures = await client.fetch()
        self.assertEqual(45, len(fixtures))
        self.assertEqual([12, 12, 12, 9], sorted(batch_sizes, reverse=True))
        self.assertEqual(2, peak)
        self.assertEqual(45, client.last_report["complete_market_fixtures"])
        self.assertTrue(all(len(f["markets"]) == 2 for f in fixtures))
        json.dumps(fixtures)

    async def test_selected_odds_refresh_keeps_full_schedule_and_marks_other_quotes_incomplete(self):
        requested = []
        async def handler(request):
            if request.url.path == DEFAULT_FIXTURE_PATH:
                return httpx.Response(200, json={"code": "0", "data": {"matchsList": [
                    match("live", ms=1), match("soon", ms=0), match("far", ms=0),
                ]}})
            body = json.loads(request.content)
            requested.extend(body["mids"].split(","))
            return httpx.Response(200, json={"code": "0", "data": {"data": [market_row(mid) for mid in body["mids"].split(",")]}})
        client = LeyuClient("https://upstream.invalid", request_id="session", transport=httpx.MockTransport(handler), batch_interval=0)
        fixtures = await client.fetch(odds_mids=lambda rows: [row["source_match_id"] for row in rows if row["source_match_id"] == "live"])
        self.assertEqual(["live"], requested)
        self.assertEqual(3, len(fixtures))
        self.assertEqual(2, len(fixtures[0]["markets"]))
        self.assertTrue(fixtures[0]["markets_complete"])
        self.assertEqual(([], False), (fixtures[1]["markets"], fixtures[1]["markets_complete"]))
        self.assertEqual(1, client.last_report["odds_requested_fixtures"])
        self.assertEqual(1, client.last_report["complete_market_fixtures"])

    async def test_missing_batch_detail_uses_v1_single_match_odds_endpoint(self):
        async def handler(request):
            if request.url.path == DEFAULT_FIXTURE_PATH:
                return httpx.Response(200, json={"code": "0", "data": {"matchsList": [match("only")]}})
            if request.url.path == DEFAULT_ODDS_PATH:
                return httpx.Response(200, json={"code": "0", "data": {"data": []}})
            self.assertEqual(DEFAULT_MATCH_ODDS_PATH, request.url.path)
            body = json.loads(request.content)
            self.assertEqual({"cuid", "cos", "orpt", "euid", "mid", "mcid", "newUser"}, set(body))
            self.assertEqual("only", body["mid"])
            return httpx.Response(200, json={"code": "0", "data": {"data": [market_row("only")]}})
        client = LeyuClient("https://upstream.invalid", request_id="session",
                            transport=httpx.MockTransport(handler), batch_interval=0)
        fixtures = await client.fetch()
        self.assertTrue(fixtures[0]["markets_complete"])
        self.assertEqual(2, len(fixtures[0]["markets"]))
        self.assertEqual(1, client.last_report["single_match_fallbacks"])

    async def test_batch_failure_is_partial_not_empty_complete_snapshot(self):
        async def handler(request):
            if request.url.path == DEFAULT_FIXTURE_PATH:
                return httpx.Response(200, json={"code": "0", "data": {"matchsList": [match()]}})
            return httpx.Response(504, json={"secret": "must-not-leak"})
        client = LeyuClient("https://upstream.invalid", request_id="session", transport=httpx.MockTransport(handler))
        fixtures = await client.fetch()
        self.assertFalse(fixtures[0]["markets_complete"])
        self.assertNotIn("must-not-leak", json.dumps(client.last_report))

    async def test_missing_market_container_not_complete(self):
        async def handler(request):
            data = {"matchsList": [match()]} if request.url.path == DEFAULT_FIXTURE_PATH else {"data": [match()]}
            return httpx.Response(200, json={"code": "0", "data": data})
        client = LeyuClient("https://upstream.invalid", request_id="session", transport=httpx.MockTransport(handler))
        self.assertFalse((await client.fetch())[0]["markets_complete"])

    async def test_auth_error_cancels_batches_instead_of_swallowing(self):
        async def handler(request):
            if request.url.path == DEFAULT_FIXTURE_PATH:
                return httpx.Response(200, json={"code": "0", "data": {"matchsList": [match()]}})
            return httpx.Response(200, json={"code": "0401013", "msg": "private"})
        client = LeyuClient("https://upstream.invalid", request_id="session", transport=httpx.MockTransport(handler))
        with self.assertRaises(AuthExpired):
            await client.fetch()

    async def test_odds_limit_preserves_schedule_and_stops_queued_requests(self):
        odds_calls = 0
        async def handler(request):
            nonlocal odds_calls
            if request.url.path == DEFAULT_FIXTURE_PATH:
                return httpx.Response(200, json={"code": "0", "data": {"matchsList": [match(str(i)) for i in range(36)]}})
            odds_calls += 1
            if odds_calls == 2:
                return httpx.Response(200, json={"code": "0401038", "msg": "secret response"})
            mids = json.loads(request.content)["mids"].split(",")
            return httpx.Response(200, json={"code": "0", "data": {"data": [market_row(mid) for mid in mids]}})
        client = LeyuClient("https://upstream.invalid", request_id="session",
                            transport=httpx.MockTransport(handler), batch_interval=0)
        fixtures = await client.fetch()
        self.assertEqual(36, len(fixtures))
        self.assertEqual(2, odds_calls)
        self.assertEqual(12, sum(bool(f["markets_complete"]) for f in fixtures))
        self.assertTrue(client.last_report["rate_limited"])
        self.assertEqual("0401038", client.last_report["rate_limit_code"])
        self.assertNotIn("secret response", json.dumps(client.last_report))

    async def test_schedule_limit_remains_failure(self):
        client = LeyuClient("https://upstream.invalid", request_id="session",
                            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"code": "0401038"})))
        with self.assertRaises(RateLimited):
            await client.fetch()

    async def test_missing_odds_fallback_has_per_cycle_request_budget(self):
        fallbacks = 0
        async def handler(request):
            nonlocal fallbacks
            if request.url.path == DEFAULT_FIXTURE_PATH:
                return httpx.Response(200, json={"code": "0", "data": {"matchsList": [match(str(i)) for i in range(30)]}})
            if request.url.path == DEFAULT_MATCH_ODDS_PATH:
                fallbacks += 1
            return httpx.Response(200, json={"code": "0", "data": {"data": []}})
        client = LeyuClient("https://upstream.invalid", request_id="session", batch_size=12,
                            transport=httpx.MockTransport(handler), batch_interval=0)
        fixtures = await client.fetch()
        self.assertEqual(30, len(fixtures))
        self.assertEqual(12, fallbacks)
        self.assertTrue(all(not f["markets_complete"] for f in fixtures))
        self.assertIn("single_match_fallback_budget_exhausted", str(client.last_report))

    async def test_http403_does_not_claim_token_expired(self):
        client = LeyuClient("https://upstream.invalid", request_id="session",
                            transport=httpx.MockTransport(lambda request: httpx.Response(403)))
        with self.assertRaises(LeyuError) as raised:
            await client.fetch()
        self.assertNotIsInstance(raised.exception, AuthExpired)
        self.assertEqual("http_access_denied", raised.exception.category)


if __name__ == "__main__":
    unittest.main()
