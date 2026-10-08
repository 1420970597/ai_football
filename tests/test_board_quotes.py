"""Board quote/decision alignment: real API behavior, no external network."""
from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from api.app import ApiApp
from core.market_labels import describe_market


class TestBoardQuotes(TestCase):
    def setUp(self):
        self.match = {"match_id": "a", "home": "A", "away": "B", "state": "active",
                      "latest_odds": {"HAD": [2.0, 3.1, 3.4]},
                      "latest_outcomes": {"HAD": ["home", "draw", "away"]},
                      "_quote_source": "live"}
        self.dec = {"match_id": "a", "has_buy": True, "computed_at": "2026-10-08T07:00:00Z",
                    "picks": [{"market": "HAD", "outcome": "home", "edge": 0.1,
                               "pick_label": "A胜"}],
                    "computations": [{"market": "HAD", "outcomes": ["home", "draw", "away"],
                                      "odds": [2.0, 3.1, 3.4], "p_fair": [0.5, 0.3, 0.2],
                                      "edges": [0.1, -0.1, -0.2], "gate_passed": True,
                                      "gates": [{"passed": True}], "margin": 0.1}]}

    def markets(self):
        return ApiApp._board_markets(self.match, self.dec, describe_market)

    def test_unchanged_quote_keeps_decision(self):
        row = self.markets()[0]
        self.assertEqual(row["odds"], [2.0, 3.1, 3.4])
        self.assertFalse(row["decision_stale"])
        self.assertTrue(row["gate_passed"])
        self.assertEqual(row["quote_source"], "live")
        self.assertEqual(row["decided_at"], self.dec["computed_at"])

    def test_changed_quote_invalidates_calculation_without_mutating_cache(self):
        before = deepcopy(self.dec)
        self.match["latest_odds"]["HAD"][0] = 2.2
        row = self.markets()[0]
        self.assertEqual(row["odds"][0], 2.2)
        self.assertEqual(row["decision_odds"][0], 2.0)
        self.assertTrue(row["decision_stale"])
        self.assertFalse(row["gate_passed"])
        self.assertEqual(row["gates"], [])
        self.assertEqual(row["edges"], [])
        self.assertEqual(self.dec, before)

    def test_new_market_is_included_as_undecided(self):
        self.match["latest_odds"]["OU(2.5)"] = [1.8, 2.1]
        self.match["latest_outcomes"]["OU(2.5)"] = ["over", "under"]
        rows = self.markets()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["odds"], [1.8, 2.1])
        self.assertFalse(rows[1]["decided"])
        self.assertFalse(rows[1]["decision_stale"])

    def test_missing_quote_retains_only_historical_prices(self):
        self.match["latest_odds"] = {}
        row = self.markets()[0]
        self.assertEqual(row["odds"], [])
        self.assertEqual(row["decision_odds"], [2.0, 3.1, 3.4])
        self.assertFalse(row["quote_available"])
        self.assertTrue(row["decision_stale"])

    def test_reordered_outcomes_do_not_misalign_prices(self):
        self.match["latest_outcomes"]["HAD"] = ["away", "draw", "home"]
        self.match["latest_odds"]["HAD"] = [3.4, 3.1, 2.0]
        row = self.markets()[0]
        self.assertTrue(row["decision_stale"])
        self.assertEqual(row["decision_odds"], [3.4, 3.1, 2.0])
        self.assertEqual(row["p_fair"], [])

    def test_changed_quote_removes_buy_from_full_and_summary_responses(self):
        self.match["latest_odds"]["HAD"][0] = 2.2
        svc = Mock()
        svc.list_matches.return_value = [self.match]
        analysis = SimpleNamespace(
            live_match_ids=lambda: {"a"}, realtime=None,
            latest_result=lambda **kw: {"decisions": [self.dec]},
            _live_source="test", _live_error="", scheduler_health=lambda: {},
            cycle_stats={})
        app = ApiApp(svc, analysis=analysis)
        for markets in ("0", "1"):
            status, data = app.dispatch("GET", "/api/v1/board", {"markets": [markets]}, {})
            self.assertEqual(status, 200)
            self.assertEqual(data["buy"], 0)
            self.assertEqual(data["matches"][0]["picks"], [])
            self.assertEqual(data["matches"][0]["gated_in"], 0)

    def test_snapshot_quotes_are_labeled(self):
        self.match.pop("_quote_source")
        self.assertEqual(self.markets()[0]["quote_source"], "snapshot")
