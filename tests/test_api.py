#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""api.app 的单元测试（README §6 契约）。

用真实 output/场次*.json 数据驱动，覆盖 11 个端点 + 错误路径。
"""

from __future__ import annotations

import json
import io
import os
import shutil
import tempfile
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from api.app import ApiApp, BadRequest, NotFound, _Handler, create_app
from core.models import OddsSnapshot
from datetime import datetime, timezone
from service.valuation import LEYU_SOURCE_NAME
from service.response_projection import ResponseProjection

#: 真实乐鱼快照离线夹具（无网、不依赖仓库 output/）。
FIXTURE_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fixtures", "leyu_snapshots")


def build_app() -> ApiApp:
    """用乐鱼快照夹具构造应用。

    数据源已统一为乐鱼，不再有「体彩语料」分支；夹具为真实采集到的
    乐鱼快照（3 场 × 多盘口 × 多时刻），复制到临时目录以免污染仓库。
    """
    tmp = tempfile.mkdtemp(prefix="apitest_")
    root = os.path.join(tmp, "snapshots")
    shutil.copytree(os.path.join(FIXTURE_ROOT, LEYU_SOURCE_NAME),
                    os.path.join(root, LEYU_SOURCE_NAME))
    return create_app(snapshot_root=root)


def call(app, verb, path, body=None, q=None):
    query = {k: [str(v)] for k, v in (q or {}).items()}
    return app.dispatch(verb, path, query, body or {})


class TestPostgresJSONResponse(unittest.TestCase):
    def handler(self):
        handler = object.__new__(_Handler)
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        return handler

    def test_decimal_aggregate_response_preserves_json_numbers(self):
        handler = self.handler()
        handler._send(200, {'overall': {'stake_units': Decimal('3.0'),
                                       'accuracy': Decimal('0.6667')}})
        data = json.loads(handler.wfile.getvalue())
        self.assertEqual(data['overall']['stake_units'], 3)
        self.assertEqual(data['overall']['accuracy'], .6667)
        handler.send_response.assert_called_once_with(200)

    def test_json_response_still_rejects_nonfinite_and_unknown_values(self):
        with self.assertRaises(ValueError):
            self.handler()._send(200, {'value': Decimal('NaN')})
        with self.assertRaises(TypeError):
            self.handler()._send(200, {'value': object()})


class TestCompactRecommendationsAPI(unittest.TestCase):
    def setUp(self):
        self.analysis = Mock()
        self.app = ApiApp(Mock(), analysis=self.analysis)
        self.source = {
            'open': [{'match_id': 'live', 'pick': {'label': '大2.5', 'odds': 1.95,
                      'decision_count': 7, 'weights': {'private': 1}},
                      'betting': {'status': 'disabled', 'reason': '投注已关闭'}}],
            'closed': [{'decision_id': str(i), 'match_id': f'match-{i // 3}',
                        'market': 'OU', 'line': '2.5', 'outcome': 'over',
                        'status': 'pending', 'pnl': 0, 'weights': {'private': 1}}
                       for i in range(1000)],
            'summary': {'open': 1, 'closed': 2300, 'pending': 2300},
            'generated_at': '2026-10-14T12:00:00Z',
        }
        self.builder = Mock(return_value=self.source)
        self.cache = ResponseProjection(self.builder)
        self.app._projection = self.cache
        self.cache.read('real', 'recommendations', {})
        self.cache.refresh()

    def test_compact_pages_read_only_the_cache_and_preserve_open_values(self):
        for offset in (0, 100, 900):
            status, result = call(self.app, 'GET', '/api/v1/recommendations',
                                  q={'compact': 1, 'closed_offset': offset})
            self.assertEqual(status, 200)
            self.assertEqual(len(result['closed']), 100)
            self.assertEqual(result['closed'][0]['decision_id'], str(offset))
            self.assertEqual(result['closed_page']['summary_total'], 2300)
            self.assertEqual(result['open'][0]['pick'],
                             {'label': '大2.5', 'odds': 1.95, 'decision_count': 7})
            self.assertEqual(result['open'][0]['betting'], self.source['open'][0]['betting'])
        self.builder.assert_called_once_with('real', 'recommendations')
        self.assertEqual(self.analysis.mock_calls, [])
        self.assertEqual(len(self.source['closed']), 1000)
        self.assertIn('weights', self.source['open'][0]['pick'])

    def test_default_and_explicit_zero_preserve_original_payload_fields_values(self):
        for query in ({}, {'compact': 0, 'closed_limit': 1, 'closed_offset': 900}):
            status, result = call(self.app, 'GET', '/api/v1/recommendations', q=query)
            self.assertEqual(status, 200)
            self.assertEqual({k: v for k, v in result.items() if k != 'projection'}, self.source)
            self.assertNotIn('closed_page', result)
            self.assertEqual(len(result['closed']), 1000)
        self.builder.assert_called_once()

    def test_cold_compact_cache_returns_loading_empty_page_without_analysis(self):
        self.app._projection = ResponseProjection(Mock())
        status, result = call(self.app, 'GET', '/api/v1/recommendations', q={'compact': 1})
        self.assertEqual(status, 200)
        self.assertTrue(result['projection']['loading'])
        self.assertEqual(result['closed_page']['total_available'], 0)
        self.assertEqual(result['closed_page']['offset'], 0)
        self.assertEqual(self.analysis.mock_calls, [])

    def test_non_projection_fallback_remains_compatible_and_opt_in(self):
        self.app._projection = None
        self.app._build_recommendations = Mock(return_value=self.source)
        status, original = call(self.app, 'GET', '/api/v1/recommendations')
        self.assertEqual(status, 200)
        self.assertIs(original, self.source)
        status, projected = call(self.app, 'GET', '/api/v1/recommendations', q={'compact': 1})
        self.assertEqual(status, 200)
        self.assertEqual(len(projected['closed']), 100)
        self.assertEqual(len(self.source['closed']), 1000)

    def test_invalid_compact_and_pagination_values_return_400(self):
        invalid = [
            {'compact': value} for value in ('', 'true', 'false', '-1', '2', '1.0')
        ] + [
            {'compact': 1, 'closed_limit': value}
            for value in ('', '0', '-1', '101', '1.5', 'abc')
        ] + [
            {'compact': 1, 'closed_offset': value} for value in ('', '-1', '.5', 'abc')
        ]
        for query in invalid:
            with self.subTest(query=query):
                status, result = call(self.app, 'GET', '/api/v1/recommendations', q=query)
                self.assertEqual(status, 400)
                self.assertIn('error', result)
        status, _ = self.app.dispatch('GET', '/api/v1/recommendations',
                                      {'compact': ['1', 'invalid']}, {})
        self.assertEqual(status, 400)
        self.builder.assert_called_once()

    def test_http_adapter_rejects_explicit_blank_page_parameters(self):
        for query in ('compact=', 'compact=1&closed_limit=', 'compact=1&closed_offset='):
            with self.subTest(query=query):
                handler = object.__new__(_Handler)
                handler.path = '/api/v1/recommendations?' + query
                handler.app = self.app
                handler._send = Mock()
                handler._handle('GET')
                self.assertEqual(handler._send.call_args.args[0], 400)

    def test_http_adapter_keeps_legacy_empty_type_query_compatibility(self):
        for query in ('type=', 'type=&type=real', 'type=&compact=1'):
            with self.subTest(query=query):
                handler = object.__new__(_Handler)
                handler.path = '/api/v1/recommendations?' + query
                handler.app = self.app
                handler._send = Mock()
                handler._handle('GET')
                self.assertEqual(handler._send.call_args.args[0], 200)


class TestHealth(unittest.TestCase):
    def setUp(self):
        self.app = build_app()

    def test_health_ok(self):
        st, body = call(self.app, "GET", "/health")
        self.assertEqual(st, 200)
        self.assertEqual(body["status"], "healthy")
        self.assertIn("snapshot_store", body)

    def test_health_rejects_post(self):
        st, _ = call(self.app, "POST", "/health", {})
        self.assertEqual(st, 405)


class TestMatches(unittest.TestCase):
    def setUp(self):
        self.app = build_app()

    def test_list(self):
        st, body = call(self.app, "GET", "/api/v1/matches")
        self.assertEqual(st, 200)
        self.assertGreater(body["count"], 0)
        m = body["matches"][0]
        for k in ("match_id", "league", "home", "away", "state"):
            self.assertIn(k, m)

    def test_filter_by_league(self):
        _, all_ = call(self.app, "GET", "/api/v1/matches")
        lg = all_["matches"][0]["league"]
        _, f = call(self.app, "GET", "/api/v1/matches", q={"league": lg})
        self.assertTrue(all(x["league"] == lg for x in f["matches"]))

    def test_filter_by_query(self):
        _, all_ = call(self.app, "GET", "/api/v1/matches")
        mid = all_["matches"][0]["match_id"]
        _, f = call(self.app, "GET", "/api/v1/matches", q={"q": mid})
        self.assertGreaterEqual(f["count"], 1)

    def test_empty_result_for_future_date(self):
        st, body = call(self.app, "GET", "/api/v1/matches",
                        q={"date": "2099-01-01"})
        self.assertEqual(st, 200)
        self.assertEqual(body["count"], 0)

    def test_detail(self):
        _, all_ = call(self.app, "GET", "/api/v1/matches")
        mid = all_["matches"][0]["match_id"]
        st, body = call(self.app, "GET", "/api/v1/matches/" + mid)
        self.assertEqual(st, 200)
        self.assertEqual(body["match_id"], mid)
        self.assertIn("snapshots", body)

    def test_detail_404(self):
        st, body = call(self.app, "GET", "/api/v1/matches/does-not-exist")
        self.assertEqual(st, 404)
        self.assertIn("error", body)

    def test_odds(self):
        _, all_ = call(self.app, "GET", "/api/v1/matches")
        mid = all_["matches"][0]["match_id"]
        st, body = call(self.app, "GET", "/api/v1/odds/" + mid)
        self.assertEqual(st, 200)
        self.assertIn("markets", body)


class TestCachedDecisionFilters(unittest.TestCase):
    def setUp(self):
        self.app = build_app()
        self.rows = [
            {"match_id": "a", "league": "英超", "home": "Arsenal", "away": "B",
             "decision": "buy", "picks": [{"outcome": "home"}]},
            {"match_id": "b", "league": "日职", "home": "C", "away": "D",
             "decision": "avoid", "picks": []},
        ]
        self.result = {"count": 2, "decisions": self.rows, "summary": {"n": 2, "buy": 1}}
        self.app._analysis = SimpleNamespace(
            latest_result=Mock(return_value=self.result),
            latest_age_s=Mock(return_value=20), cycle_stats={},
            start_job=Mock(side_effect=AssertionError("Read must not call LLM")))

    def test_keyword_casefold_and_summary(self):
        status, data = call(self.app, "GET", "/api/v1/decisions", q={"q": "ARSENAL"})
        self.assertEqual(status, 200)
        self.assertEqual(data["decisions"], [self.rows[0]])
        self.assertEqual(data["summary"]["buy"], 1)
        self.assertEqual(data["summary"]["n"], 1)
        self.assertEqual(len(self.result["decisions"]), 2)

    def test_league_and_empty(self):
        _, data = call(self.app, "GET", "/api/v1/decisions", q={"league": "日职"})
        self.assertEqual(data["decisions"], [self.rows[1]])
        _, empty = call(self.app, "GET", "/api/v1/decisions", q={"q": "不存在"})
        self.assertEqual(empty["count"], 0)
        self.assertEqual(empty["summary"]["buy"], 0)
        self.assertEqual(empty["summary"]["n"], 0)
        self.app.analysis.start_job.assert_not_called()

    def test_date_intersects_local_snapshot_matches(self):
        self.app.svc.list_matches = Mock(return_value=[{"match_id": "b"}])
        _, data = call(self.app, "GET", "/api/v1/decisions", q={"date": "2026-10-08"})
        self.assertEqual(data["decisions"], [self.rows[1]])
        self.app.svc.list_matches.assert_called_once_with(date="2026-10-08")


class TestFairEdge(unittest.TestCase):
    def setUp(self):
        self.app = build_app()
        _, all_ = call(self.app, "GET", "/api/v1/matches")
        self.mid = all_["matches"][0]["match_id"]

    def test_fair(self):
        st, body = call(self.app, "GET", "/api/v1/fair/" + self.mid)
        self.assertEqual(st, 200)
        self.assertIn("probabilities", body)
        self.assertIn("method_spread_pp", body)
        self.assertAlmostEqual(sum(body["probabilities"]), 1.0, places=6)
        self.assertEqual(len(body["odds"]), len(body["outcomes"]))

    def test_fair_fallback_prices_match_actual_market(self):
        snap = OddsSnapshot(
            match_id="fallback", league="L", home="A", away="B", market="OU(2.5)",
            outcomes=("over", "under"), odds=(1.8, 2.1),
            source="leyu", captured_at=datetime.now(timezone.utc))
        self.app.svc._latest = Mock(return_value=snap)
        _, data = call(self.app, "GET", "/api/v1/fair/fallback")
        self.assertTrue(data["market_fallback"])
        self.assertEqual(data["market"], "OU(2.5)")
        self.assertEqual(data["odds"], [1.8, 2.1])
        self.assertEqual(data["outcomes"], ["over", "under"])

    def test_fair_method_param(self):
        for m in ("shin", "proportional", "additive", "power", "odds_ratio"):
            st, body = call(self.app, "GET", "/api/v1/fair/" + self.mid,
                            q={"method": m})
            self.assertEqual(st, 200, m)

    def test_fair_invalid_method_falls_back(self):
        st, body = call(self.app, "GET", "/api/v1/fair/" + self.mid,
                        q={"method": "nonexistent"})
        self.assertEqual(st, 200)

    def test_fair_ev_is_negative(self):
        """报告 §9.1：公平定价下 EV = −m/(1+m) 必为负。"""
        _, body = call(self.app, "GET", "/api/v1/fair/" + self.mid)
        self.assertLess(body["ev_if_fair"], 0.0)

    def test_fair_404(self):
        st, _ = call(self.app, "GET", "/api/v1/fair/99999999")
        self.assertEqual(st, 404)

    def test_edge_zero_information_is_negative(self):
        """核心诚实性：未提供 p_model 时 edge 必为负（报告 §8.2）。"""
        st, body = call(self.app, "GET", "/api/v1/edge/" + self.mid)
        self.assertEqual(st, 200)
        self.assertTrue(all(e < 0 for e in body["edge"]))
        self.assertTrue(any("零信息" in n or "公平概率" in n
                            for n in body["notes"]))

    def test_edge_execution_not_supported(self):
        _, body = call(self.app, "POST", "/api/v1/portfolio",
                       {"match_id": self.mid})
        self.assertEqual(body["execution"]["auto_betting"], "NOT_SUPPORTED")

    def test_edge_bad_q_fill(self):
        st, body = call(self.app, "GET", "/api/v1/edge/" + self.mid,
                        q={"q_fill": "2"})
        self.assertEqual(st, 400)
        self.assertIn("q_fill", body["error"])

    def test_edge_bad_n_obs(self):
        st, _ = call(self.app, "GET", "/api/v1/edge/" + self.mid,
                     q={"n_obs": "abc"})
        self.assertEqual(st, 400)


class TestMicrostructure(unittest.TestCase):
    def setUp(self):
        self.app = build_app()
        _, all_ = call(self.app, "GET", "/api/v1/matches")
        self.mid = all_["matches"][0]["match_id"]

    def test_microstructure(self):
        st, body = call(self.app, "GET", "/api/v1/microstructure/" + self.mid)
        self.assertEqual(st, 200)
        self.assertIn("signals", body)
        self.assertGreater(len(body["signals"]), 0)

    def test_404(self):
        st, _ = call(self.app, "GET", "/api/v1/microstructure/99999999")
        self.assertEqual(st, 404)


class TestPortfolio(unittest.TestCase):
    def setUp(self):
        self.app = build_app()
        _, all_ = call(self.app, "GET", "/api/v1/matches")
        self.mid = all_["matches"][0]["match_id"]

    def test_portfolio(self):
        st, body = call(self.app, "POST", "/api/v1/portfolio",
                        {"match_id": self.mid, "rho": 0.4, "lam": 0.25})
        self.assertEqual(st, 200)
        self.assertIn("fractions", body)
        self.assertIn("disclaimer", body)

    def test_missing_match_id(self):
        st, body = call(self.app, "POST", "/api/v1/portfolio", {})
        self.assertEqual(st, 400)
        self.assertIn("match_id", body["error"])

    def test_invalid_lam(self):
        st, _ = call(self.app, "POST", "/api/v1/portfolio",
                     {"match_id": self.mid, "lam": 5.0})
        self.assertEqual(st, 400)

    def test_invalid_rho(self):
        st, _ = call(self.app, "POST", "/api/v1/portfolio",
                     {"match_id": self.mid, "rho": 0.99 + 1e-9})
        self.assertEqual(st, 400)

    def test_non_numeric_lam(self):
        st, _ = call(self.app, "POST", "/api/v1/portfolio",
                     {"match_id": self.mid, "lam": "abc"})
        self.assertEqual(st, 400)

    def test_bool_lam_rejected(self):
        st, _ = call(self.app, "POST", "/api/v1/portfolio",
                     {"match_id": self.mid, "lam": True})
        self.assertEqual(st, 400)


class TestCalibration(unittest.TestCase):
    def setUp(self):
        self.app = build_app()

    def test_calibration(self):
        st, body = call(self.app, "GET", "/api/v1/calibration")
        self.assertEqual(st, 200)
        for k in ("n", "brier", "ece", "clv_mean", "log_growth",
                  "max_drawdown", "note"):
            self.assertIn(k, body)

    def test_invalid_n_bins(self):
        st, _ = call(self.app, "GET", "/api/v1/calibration",
                     q={"n_bins": "0"})
        self.assertEqual(st, 400)


class TestRouting(unittest.TestCase):
    def setUp(self):
        self.app = build_app()

    def test_unknown_endpoint_404(self):
        st, body = call(self.app, "GET", "/api/v1/nonexistent")
        self.assertEqual(st, 404)
        self.assertIn("error", body)

    def test_unknown_root_404(self):
        st, _ = call(self.app, "GET", "/nope")
        self.assertEqual(st, 404)

    def test_method_not_allowed(self):
        st, body = call(self.app, "POST", "/api/v1/matches", {})
        self.assertEqual(st, 405)
        self.assertIn("allowed", body)

    def test_trailing_slash_tolerated(self):
        st, _ = call(self.app, "GET", "/api/v1/matches/")
        self.assertEqual(st, 200)

    def test_url_encoded_id(self):
        st, _ = call(self.app, "GET", "/api/v1/matches/%E6%B5%8B%E8%AF%95")
        self.assertEqual(st, 404)   # 不存在，但路由应正确解析（非 500）

    def test_all_responses_json_serializable(self):
        for verb, path, body in [
            ("GET", "/health", None),
            ("GET", "/api/v1/matches", None),
            ("GET", "/api/v1/calibration", None),
        ]:
            st, payload = call(self.app, verb, path, body)
            json.dumps(payload, allow_nan=False)   # 不得含 NaN/Inf


class TestExceptions(unittest.TestCase):
    def test_bad_request_and_not_found_are_distinct(self):
        self.assertTrue(issubclass(BadRequest, ValueError))
        self.assertTrue(issubclass(NotFound, LookupError))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestBoardEndpoint(unittest.TestCase):
    """`/board`：逐场逐盘口的实时看板（用户要求的视图数据源）。

    关键契约（前端依赖）：
      * 一次请求给齐「场次 + 每个盘口的最新赔率 + 走势 + 门控结论」；
      * 该场**没有决策结果时要如实标注 decided=False**，
        而不是伪造一个通过 —— 否则用户会以为看到的是经过算法判定的信息。
      * 必须读缓存，不触发 LLM（可高频轮询）。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = build_app()

    def test_board_shape(self):
        st, d = call(self.app, "GET", "/api/v1/board", q={"markets": 1})
        self.assertEqual(st, 200)
        for k in ("generated_at", "count", "live", "buy", "decided",
                  "matches", "realtime"):
            self.assertIn(k, d)

    def test_board_rows_have_market_detail(self):
        st, d = call(self.app, "GET", "/api/v1/board", q={"markets": 1})
        self.assertEqual(st, 200)
        self.assertTrue(d["matches"])
        row = d["matches"][0]
        for k in ("match_id", "league", "home", "away", "is_live",
                  "decided", "has_buy", "markets", "gated_in"):
            self.assertIn(k, row)
        self.assertTrue(row["markets"])
        mk = row["markets"][0]
        for k in ("market", "label", "outcomes", "odds", "p_fair",
                  "edges", "trend", "gate_passed", "decided"):
            self.assertIn(k, mk)

    def test_undecided_rows_are_honestly_marked(self):
        """没有决策结果的场次必须 decided=False（不得伪装成已判定）。"""
        st, d = call(self.app, "GET", "/api/v1/board", q={"markets": 1})
        self.assertEqual(st, 200)
        for row in d["matches"]:
            if not row["decided"]:
                self.assertFalse(row["has_buy"])
                self.assertEqual(row["picks"], [])
                for mk in row["markets"]:
                    self.assertFalse(mk["decided"])

    def test_markets_zero_omits_detail(self):
        st, d = call(self.app, "GET", "/api/v1/board", q={"markets": 0})
        self.assertEqual(st, 200)
        if d["matches"]:
            self.assertNotIn("markets", d["matches"][0])

    def test_board_does_not_trigger_llm(self):
        """必须读缓存：连续调用不应增加 LLM 调用数。"""
        before = self.app.analysis.llm_health().get("calls", 0)
        for _ in range(3):
            call(self.app, "GET", "/api/v1/board")
        after = self.app.analysis.llm_health().get("calls", 0)
        self.assertEqual(before, after)
