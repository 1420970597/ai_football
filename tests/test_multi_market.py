#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多玩法采集与聚合的单元测试。

覆盖：
  - collector.normalizer.parse_pooled_odds：从官方 oddsList 解析全部玩法
  - service.valuation.ValuationService.list_markets / get_model_probs /
    cross_market_check / market_catalog
  - 遗留市场名（1X2/AH）与新目录名（HAD/HHAD）的映射

重点回归的**真实缺陷**：
  1. 采集器原先只认 HAD/HHAD，静默丢弃 TTG/CRS/HAFU 赔率
  2. GoalLine 缺失时产生噪音告警
  3. edge 公式误写为 (p_model − p_market) × odds，而非 p_model × odds − 1
  4. 遗留市场名导致「模型 vs 市场」对照永远找不到快照
"""

from __future__ import annotations

import unittest

from collector.normalizer import parse_pooled_odds
from core import markets as mk
from service.valuation import ValuationService


def _official_sample() -> dict:
    """构造一份覆盖 5 种官方玩法的 oddsList 样本。"""
    return {
        "matchNum": 2001,
        "leagueAbbName": "日职",
        "leagueAllName": "日本职业联赛",
        "homeTeamAbbName": "鹿岛鹿角",
        "awayTeamAbbName": "大阪樱花",
        "matchDate": "2025-09-23",
        "matchTime": "18:00",
        "matchStatus": "Selling",
        "updateTime": "2025-09-23T13:42:18",
        "oddsList": [
            {"poolCode": "HAD", "h": "2.13", "d": "3.45", "a": "2.70"},
            {"poolCode": "HHAD", "h": "4.50", "d": "3.75", "a": "1.56",
             "goalLine": "-1.00"},
            {"poolCode": "TTG", "s0": "9.00", "s1": "4.60", "s2": "3.30",
             "s3": "3.70", "s4": "6.00", "s5": "12.0", "s6": "26.0",
             "s7": "35.0"},
            {"poolCode": "HAFU", "hh": "3.30", "hd": "15.0", "ha": "30.0",
             "dh": "5.60", "dd": "6.40", "da": "9.50", "ah": "17.0",
             "ad": "15.0", "aa": "4.40"},
            {"poolCode": "CRS",
             "1:0": "7.50", "2:0": "11.0", "2:1": "9.00", "3:0": "26.0",
             "3:1": "21.0", "3:2": "34.0", "4:0": "70.0", "4:1": "60.0",
             "4:2": "90.0", "5:0": "200", "5:1": "150", "5:2": "250",
             "0:0": "9.50", "1:1": "6.00", "2:2": "17.0", "3:3": "70.0",
             "0:1": "9.00", "0:2": "17.0", "1:2": "13.0", "0:3": "40.0",
             "1:3": "35.0", "2:3": "50.0", "0:4": "110", "1:4": "90.0",
             "2:4": "150", "0:5": "300", "1:5": "200", "2:5": "350",
             "胜其他": "25.0", "平其他": "60.0", "负其他": "35.0"},
        ],
    }


class TestParsePooledOdds(unittest.TestCase):
    """从官方 oddsList 解析全部玩法。"""

    def test_parses_all_five_official_markets(self):
        snaps = parse_pooled_odds(_official_sample())
        codes = {s.market for s in snaps}
        self.assertEqual(
            codes, {"HAD", "HHAD(-1)", "TTG", "CRS", "HAFU"})

    def test_no_noise_issues_on_clean_input(self):
        """干净输入不应产生任何告警（goalLine 是可选字段）。"""
        issues: list = []
        parse_pooled_odds(_official_sample(), issues=issues)
        self.assertEqual([str(i) for i in issues], [])

    def test_outcome_counts(self):
        snaps = {s.market: s for s in parse_pooled_odds(_official_sample())}
        self.assertEqual(snaps["HAD"].n_outcomes, 3)
        self.assertEqual(snaps["TTG"].n_outcomes, 8)
        self.assertEqual(snaps["HAFU"].n_outcomes, 9)
        self.assertEqual(snaps["CRS"].n_outcomes, 31)

    def test_odds_are_floats_not_strings(self):
        """回归：上游赔率是字符串，必须归一为 float，否则除法会崩。"""
        for s in parse_pooled_odds(_official_sample()):
            for o in s.odds:
                self.assertIsInstance(o, float)
            self.assertGreater(s.booksum, 0.0)

    def test_outcome_order_matches_spec(self):
        snaps = {s.market: s for s in parse_pooled_odds(_official_sample())}
        self.assertEqual(snaps["TTG"].outcomes, mk.TTG.outcomes)
        self.assertEqual(snaps["HAFU"].outcomes, mk.HAFU.outcomes)
        self.assertEqual(snaps["CRS"].outcomes, mk.CRS.outcomes)

    def test_handicap_line_recorded(self):
        snaps = {s.market: s for s in parse_pooled_odds(_official_sample())}
        self.assertEqual(snaps["HHAD(-1)"].metadata.get("盘口"), -1.0)

    def test_unknown_key_is_reported_not_silently_dropped(self):
        """未知赔率键必须告警（fail-loud），否则会把缺口伪装成低水钱。"""
        raw = _official_sample()
        raw["oddsList"] = [{"poolCode": "HAD", "h": "2.13", "d": "3.45",
                            "a": "2.70", "XX": "9.9"}]
        issues: list = []
        parse_pooled_odds(raw, issues=issues)
        self.assertTrue(any("XX" in str(i) for i in issues),
                        [str(i) for i in issues])

    def test_unknown_pool_code_yields_no_snapshot(self):
        raw = _official_sample()
        raw["oddsList"] = [{"poolCode": "ZZZ", "x": "1.5"}]
        issues: list = []
        snaps = parse_pooled_odds(raw, issues=issues)
        self.assertEqual(snaps, [])
        self.assertTrue(issues)

    def test_missing_required_odds_skips_market(self):
        raw = _official_sample()
        raw["oddsList"] = [{"poolCode": "HAD", "h": "2.13", "d": "3.45"}]
        issues: list = []
        snaps = parse_pooled_odds(raw, issues=issues)
        self.assertEqual(snaps, [])
        self.assertTrue(any("不完整" in str(i) for i in issues))

    def test_empty_odds_list_is_reported(self):
        raw = _official_sample()
        raw["oddsList"] = []
        issues: list = []
        self.assertEqual(parse_pooled_odds(raw, issues=issues), [])
        self.assertTrue(any("为空" in str(i) for i in issues))

    def test_missing_match_id(self):
        raw = _official_sample()
        del raw["matchNum"]
        issues: list = []
        self.assertEqual(parse_pooled_odds(raw, issues=issues), [])
        self.assertTrue(issues)

    def test_missing_team_names(self):
        raw = _official_sample()
        raw["homeTeamAbbName"] = ""
        raw["homeTeamAllName"] = ""
        issues: list = []
        self.assertEqual(parse_pooled_odds(raw, issues=issues), [])
        self.assertTrue(issues)

    def test_non_mapping_entry(self):
        raw = _official_sample()
        raw["oddsList"] = ["not-a-dict"]
        issues: list = []
        self.assertEqual(parse_pooled_odds(raw, issues=issues), [])
        self.assertTrue(issues)

    def test_every_market_is_deviggable(self):
        """关键：每种玩法都要能独立去水（这是多玩法统计的前提）。"""
        from core import devig
        for s in parse_pooled_odds(_official_sample()):
            fp = devig.devig(s)
            ok, _ = mk.partition_check(
                mk.spec_by_code(s.market), fp.probabilities)
            self.assertTrue(ok, s.market)

    def test_margins_differ_across_markets(self):
        """不同玩法的水钱不同 —— 这正是「多玩法统计」的价值所在。

        实测：HAD ≈13%，CRS ≈30%。若各玩法水钱相同，说明解析串味了。
        """
        snaps = {s.market: s for s in parse_pooled_odds(_official_sample())}
        self.assertLess(snaps["HAD"].margin, snaps["CRS"].margin)
        self.assertLess(snaps["HAD"].margin, snaps["TTG"].margin)


class TestServiceMultiMarket(unittest.TestCase):
    """服务层的多玩法聚合（用合成语料，不依赖仓库 output/）。"""

    def _service(self) -> ValuationService:
        # 显式指定 ticai：这些用例验证的是**体彩语料**的解析与聚合，
        # 不能受默认数据源（乐鱼）影响，否则会去打网络。
        return ValuationService(snapshot_root="output",
                                corpus_root="output",
                                prefer_redis=False,
                                source="ticai")

    def setUp(self):
        self.svc = self._service()
        try:
            self.svc.ingest_corpus()
        except (OSError, FileNotFoundError):
            self.skipTest("output/ 语料不可用")

    def _any_match(self) -> str:
        res = self.svc.list_matches()
        matches = res if isinstance(res, list) else res.get("matches", [])
        if not matches:
            self.skipTest("无语料")
        return matches[0]["match_id"]

    def test_catalog_lists_nine_markets(self):
        cat = self.svc.market_catalog()
        self.assertEqual(cat["n_markets"], 9)
        codes = {m["code"] for m in cat["markets"]}
        self.assertIn("CRS", codes)
        self.assertIn("HAFU", codes)

    def test_catalog_documents_data_gap(self):
        """目录必须诚实说明数据缺口，否则用户会以为 CRS 无数据是 bug。"""
        note = self.svc.market_catalog()["note"]
        self.assertIn("缺口", note)

    def test_list_markets_returns_rows(self):
        mid = self._any_match()
        d = self.svc.list_markets(mid)
        assert d is not None
        self.assertGreater(d["n_markets"], 0)
        for row in d["markets"]:
            self.assertIn("margin", row)
            self.assertIn("ev_if_fair", row)
            self.assertIn("method_spread_pp", row)

    def test_list_markets_sorted_by_margin(self):
        mid = self._any_match()
        d = self.svc.list_markets(mid)
        assert d is not None
        margins = [r["margin"] for r in d["markets"]]
        self.assertEqual(margins, sorted(margins))

    def test_list_markets_unknown_match(self):
        self.assertIsNone(self.svc.list_markets("no-such-match"))

    def test_ev_matches_margin_formula(self):
        """EV 必须等于 −m/(1+m)，这是报告的核心恒等式。"""
        mid = self._any_match()
        d = self.svc.list_markets(mid)
        assert d is not None
        for row in d["markets"]:
            m = row["margin"]
            if row.get("ev_if_fair") is None:
                continue
            self.assertAlmostEqual(row["ev_if_fair"], -m / (1.0 + m),
                                   places=8)

    def test_fair_odds_are_reciprocal_of_probabilities(self):
        mid = self._any_match()
        d = self.svc.list_markets(mid)
        assert d is not None
        for row in d["markets"]:
            for p, fo in zip(row["probabilities"], row["fair_odds"]):
                if p > 0:
                    self.assertAlmostEqual(fo, 1.0 / p, places=5)

    def test_market_detail_has_results(self):
        mid = self._any_match()
        d = self.svc.get_market(mid, "HAD")
        assert d is not None
        self.assertEqual(len(d["results"]), d["n_outcomes"])
        for r in d["results"]:
            self.assertIn("fair_prob", r)
            self.assertIn("raw_implied", r)

    def test_legacy_market_name_resolves(self):
        """回归：遗留名 1X2 必须能解析到规范名 HAD 的快照。"""
        mid = self._any_match()
        legacy = self.svc.get_market(mid, "1X2")
        modern = self.svc.get_market(mid, "HAD")
        assert legacy is not None and modern is not None
        self.assertEqual(legacy["margin"], modern["margin"])

    def test_legacy_alias_not_double_counted(self):
        """1X2 与 HAD 是同一玩法，list_markets 中不应出现两次。"""
        mid = self._any_match()
        d = self.svc.list_markets(mid)
        assert d is not None
        names = [r["market"] for r in d["markets"]]
        self.assertEqual(len(names), len(set(names)))

    def test_model_probs_all_five_official_markets(self):
        mid = self._any_match()
        for code in ("HAD", "HHAD(-1)", "TTG", "CRS", "HAFU"):
            r = self.svc.get_model_probs(mid, code)
            assert r is not None
            if "error" in r:
                continue
            self.assertTrue(r["partition_ok"], code)
            self.assertAlmostEqual(
                sum(r["model_probabilities"]),
                mk.spec_by_code(code).space.expected_sum, places=6)

    def test_model_probs_formula_is_p_times_odds_minus_one(self):
        """回归：edge 必须是 p_model × odds − 1。

        曾误写为 (p_model − p_market) × odds，那是个无意义的量，
        且恒为负，会掩盖真实的正期望机会。
        """
        mid = self._any_match()
        r = self.svc.get_model_probs(mid, "HAD")
        assert r is not None
        if "model_edge_vs_market" not in r:
            self.skipTest("该场无市场对照")
        for i, e in enumerate(r["model_edge_vs_market"]):
            p = r["model_probabilities"][i]
            be = r["breakeven_prob"][i]
            self.assertAlmostEqual(e, p / be - 1.0, places=6)

    def test_model_probs_market_comparison_present(self):
        """回归：遗留名映射修复后，模型必须能找到市场对照。"""
        mid = self._any_match()
        r = self.svc.get_model_probs(mid, "HAD")
        assert r is not None
        self.assertIn("market_probabilities", r)
        self.assertEqual(len(r["market_probabilities"]),
                         len(r["model_probabilities"]))

    def test_model_probs_rejects_unknown_market(self):
        mid = self._any_match()
        r = self.svc.get_model_probs(mid, "BOGUS")
        assert r is not None
        self.assertIn("error", r)

    def test_model_probs_absent_for_unknown_match(self):
        self.assertIsNone(self.svc.get_model_probs("no-such-match", "HAD"))

    def test_model_probs_documents_caveats(self):
        """模型必须自带局限声明（独立泊松假设）。"""
        mid = self._any_match()
        r = self.svc.get_model_probs(mid, "HAD")
        assert r is not None
        self.assertIn("caveats", r)
        self.assertTrue(any("泊松" in c for c in r["caveats"]))

    def test_consistency_check_runs(self):
        mid = self._any_match()
        d = self.svc.cross_market_check(mid)
        assert d is not None
        self.assertIn("n_inconsistencies", d)
        self.assertIn("interpretation", d)

    def test_consistency_unknown_match(self):
        self.assertIsNone(self.svc.cross_market_check("no-such-match"))

    def test_bad_snapshot_dir_does_not_crash_catalog(self):
        svc = ValuationService(snapshot_root="/nonexistent",
                               prefer_redis=False)
        self.assertEqual(svc.market_catalog()["n_markets"], 9)


if __name__ == "__main__":
    unittest.main()
