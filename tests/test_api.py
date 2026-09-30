#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""api.app 的单元测试（README §6 契约）。

用真实 output/场次*.json 数据驱动，覆盖 11 个端点 + 错误路径。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from api.app import ApiApp, BadRequest, NotFound, create_app
from service.valuation import ValuationService

CORPUS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "output")


def build_app() -> ApiApp:
    tmp = tempfile.mkdtemp(prefix="apitest_")
    app = create_app(snapshot_root=tmp, corpus_root=CORPUS)
    app.svc.ingest_corpus()
    return app


def call(app, verb, path, body=None, q=None):
    query = {k: [str(v)] for k, v in (q or {}).items()}
    return app.dispatch(verb, path, query, body or {})


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


class TestCollectAndTasks(unittest.TestCase):
    def setUp(self):
        self.app = build_app()

    def test_collect_missing_urls(self):
        st, _ = call(self.app, "POST", "/api/v1/collect", {})
        self.assertEqual(st, 400)

    def test_collect_empty_urls(self):
        st, _ = call(self.app, "POST", "/api/v1/collect", {"urls": []})
        self.assertEqual(st, 400)

    def test_collect_rejects_non_string(self):
        st, _ = call(self.app, "POST", "/api/v1/collect", {"urls": [1, 2]})
        self.assertEqual(st, 400)

    def test_collect_too_many(self):
        st, _ = call(self.app, "POST", "/api/v1/collect",
                     {"urls": ["https://e.com"] * 51})
        self.assertEqual(st, 400)

    def test_task_not_found(self):
        st, body = call(self.app, "GET", "/api/v1/tasks/nope")
        self.assertEqual(st, 404)
        self.assertIn("error", body)


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
