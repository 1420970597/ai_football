#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多玩法市场目录与派生市场的单元测试。

覆盖 core/markets.py：市场规格、Poisson 比分模型、
从比分矩阵派生各玩法概率、跨玩法边际一致性校验、
以及上游赔率键解析（含歧义拒绝）。
"""

from __future__ import annotations

import math
import unittest
from typing import cast

from core import markets as mk


class TestMarketSpec(unittest.TestCase):
    """市场规格的基本约束。"""

    def test_catalog_covers_official_five(self):
        codes = {sp.pool_code for sp in mk.catalog() if sp.pool_code}
        for pool in ("HAD", "HHAD", "TTG", "CRS", "HAFU"):
            self.assertIn(pool, codes)

    def test_official_specs_count(self):
        self.assertEqual(len(mk.OFFICIAL_SPECS), 5)

    def test_only_dc_is_overlapping(self):
        """目录中除双重机会外，其余玩法都是完备划分。"""
        for sp in mk.catalog():
            if sp.code == "DC":
                self.assertFalse(sp.is_partition, sp.code)
            else:
                self.assertTrue(sp.is_partition, sp.code)

    def test_ttg_has_eight_outcomes(self):
        self.assertEqual(mk.TTG.n_outcomes, 8)
        self.assertIn("7+", mk.TTG.outcomes)

    def test_crs_has_thirty_one_outcomes(self):
        self.assertEqual(mk.CRS.n_outcomes, 31)
        for bucket in ("胜其他", "平其他", "负其他"):
            self.assertIn(bucket, mk.CRS.outcomes)

    def test_hafu_has_nine_outcomes(self):
        self.assertEqual(mk.HAFU.n_outcomes, 9)
        self.assertEqual(mk.HAFU.outcomes[0], "H/H")
        self.assertEqual(mk.HAFU.outcomes[8], "A/A")

    def test_duplicate_outcomes_rejected(self):
        with self.assertRaises(ValueError):
            mk.MarketSpec(code="X", name="x", outcomes=("a", "a"))

    def test_partition_needs_two_outcomes(self):
        with self.assertRaises(ValueError):
            mk.MarketSpec(code="X", name="x", outcomes=("a",))

    def test_empty_code_rejected(self):
        with self.assertRaises(ValueError):
            mk.MarketSpec(code="", name="x", outcomes=("a", "b"))

    def test_index_of_supports_aliases(self):
        self.assertEqual(mk.HAD.index_of("h"), 0)
        self.assertEqual(mk.HAD.index_of("主胜"), 0)
        self.assertEqual(mk.OU(2.5).index_of("大"), 0)

    def test_index_of_unknown_raises_keyerror(self):
        with self.assertRaises(KeyError):
            mk.HAD.index_of("nope")

    def test_line_formatting(self):
        self.assertEqual(mk.HHAD(-1.0).code, "HHAD(-1)")
        self.assertEqual(mk.OU(2.5).code, "OU(2.5)")
        self.assertEqual(mk.AH(-0.5).code, "AH(-0.5)")

    def test_spec_by_code_pool_code(self):
        self.assertIs(mk.spec_by_code("HAD"), mk.HAD)
        self.assertIs(mk.spec_by_code("TTG"), mk.TTG)
        self.assertEqual(mk.spec_by_code("HHAD").line, -1.0)

    def test_spec_by_code_rejects_empty(self):
        with self.assertRaises(ValueError):
            mk.spec_by_code("")
        with self.assertRaises(KeyError):
            mk.spec_by_code("BOGUS")

    def test_as_dict_is_json_friendly(self):
        d = mk.HAD.as_dict()
        self.assertEqual(d["code"], "HAD")
        self.assertEqual(d["n_outcomes"], 3)
        self.assertIsInstance(d["outcomes"], list)


class TestPoisson(unittest.TestCase):
    """泊松基础函数。"""

    def test_pmf_sums_to_one(self):
        lam = 1.7
        total = math.fsum(mk.poisson_pmf(k, lam) for k in range(60))
        self.assertAlmostEqual(total, 1.0, places=12)

    def test_pmf_zero_lambda(self):
        self.assertEqual(mk.poisson_pmf(0, 0.0), 1.0)
        self.assertEqual(mk.poisson_pmf(3, 0.0), 0.0)

    def test_pmf_rejects_negative(self):
        with self.assertRaises(ValueError):
            mk.poisson_pmf(-1, 1.0)
        with self.assertRaises(ValueError):
            mk.poisson_pmf(1, -1.0)

    def test_pmf_matches_closed_form(self):
        lam = 2.3
        self.assertAlmostEqual(mk.poisson_pmf(3, lam),
                               math.exp(-lam) * lam ** 3 / 6.0, places=14)

    def test_score_matrix_is_product_of_marginals(self):
        lam_h, lam_a = 1.6, 1.1
        m = mk.score_matrix(lam_h, lam_a, 6)
        self.assertAlmostEqual(m[2][1],
                               mk.poisson_pmf(2, lam_h) * mk.poisson_pmf(1, lam_a),
                               places=15)

    def test_score_matrix_total_near_one(self):
        m = mk.score_matrix(1.6, 1.1, 15)
        total = math.fsum(math.fsum(row) for row in m)
        self.assertAlmostEqual(total, 1.0, places=9)

    def test_score_matrix_rejects_negative_lambda(self):
        with self.assertRaises(ValueError):
            mk.score_matrix(-1.0, 1.0)

    def test_score_matrix_rejects_bad_max_goals(self):
        with self.assertRaises(ValueError):
            mk.score_matrix(1.0, 1.0, 0)


class TestMarketProbs(unittest.TestCase):
    """从比分矩阵派生的各玩法概率。"""

    LAM_H = 1.55
    LAM_A = 1.20

    def test_all_markets_sum_to_expected(self):
        """完备划分和为 1；重叠空间（双重机会）和为 2。"""
        for spec in mk.catalog():
            vec = mk.market_probs(spec, self.LAM_H, self.LAM_A)
            ok, total = mk.partition_check(spec, vec)
            self.assertTrue(ok, "%s 和为 %r" % (spec.code, total))
            self.assertAlmostEqual(total, spec.space.expected_sum,
                                   places=10)

    def test_dc_is_overlapping_not_partition(self):
        """双重机会结果互相重叠，**不可**当作完备划分归一化。

        回归测试：曾误把 DC 归一化到 1，把所有概率静默砍半。
        """
        self.assertIs(mk.DC.space, mk.ResultSpace.OVERLAPPING)
        self.assertFalse(mk.DC.is_partition)
        self.assertEqual(mk.DC.space.expected_sum, 2.0)
        dc = mk.market_probs(mk.DC, self.LAM_H, self.LAM_A)
        self.assertAlmostEqual(math.fsum(dc), 2.0, places=10)

    def test_had_matches_manual_computation(self):
        mg = 15
        m = mk.score_matrix(self.LAM_H, self.LAM_A, mg)
        total = math.fsum(math.fsum(r) for r in m)
        h = math.fsum(m[i][j] for i in range(mg + 1)
                      for j in range(mg + 1) if i > j) / total
        vec = mk.market_probs(mk.HAD, self.LAM_H, self.LAM_A, max_goals=mg)
        self.assertAlmostEqual(vec[0], h, places=14)

    def test_had_monotone_in_home_strength(self):
        prev = -1.0
        for lam in (0.6, 1.0, 1.4, 1.8, 2.2):
            p = mk.market_probs(mk.HAD, lam, 1.2)[0]
            self.assertGreater(p, prev)
            prev = p

    def test_crs_folds_back_to_had(self):
        """比分玩法按胜/平/负聚合后必须精确等于胜平负。"""
        had = mk.market_probs(mk.HAD, self.LAM_H, self.LAM_A)
        crs = mk.market_probs(mk.CRS, self.LAM_H, self.LAM_A)
        n_home = 12   # _CRS_HOME 的个数
        n_draw = 4
        home = math.fsum(crs[:n_home]) + crs[28]
        draw = math.fsum(crs[n_home:n_home + n_draw]) + crs[29]
        away = math.fsum(crs[n_home + n_draw:28]) + crs[30]
        self.assertAlmostEqual(home, had[0], places=10)
        self.assertAlmostEqual(draw, had[1], places=10)
        self.assertAlmostEqual(away, had[2], places=10)

    def test_ttg_matches_score_matrix_totals(self):
        mg = 15
        m = mk.score_matrix(self.LAM_H, self.LAM_A, mg)
        total = math.fsum(math.fsum(r) for r in m)
        ttg = mk.market_probs(mk.TTG, self.LAM_H, self.LAM_A, max_goals=mg)
        for k in range(7):
            expected = math.fsum(m[i][k - i] for i in range(k + 1)) / total
            self.assertAlmostEqual(ttg[k], expected, places=12)
        tail = math.fsum(m[i][j] for i in range(mg + 1)
                         for j in range(mg + 1) if i + j >= 7) / total
        self.assertAlmostEqual(ttg[7], tail, places=12)

    def test_hafu_full_time_marginals_match_had(self):
        """半全场按**全场**结果聚合应约等于胜平负（模型近似精度内）。

        注意维度：标签第一个字母是半场，第二个才是全场。
        """
        had = mk.market_probs(mk.HAD, self.LAM_H, self.LAM_A)
        hafu = mk.market_probs(mk.HAFU, self.LAM_H, self.LAM_A)
        home = hafu[0] + hafu[3] + hafu[6]
        draw = hafu[1] + hafu[4] + hafu[7]
        away = hafu[2] + hafu[5] + hafu[8]
        for got, want in ((home, had[0]), (draw, had[1]), (away, had[2])):
            self.assertAlmostEqual(got, want, places=4)

    def test_hhad_shifts_probability_to_away(self):
        had = mk.market_probs(mk.HAD, self.LAM_H, self.LAM_A)
        hhad = mk.market_probs(mk.HHAD(-1), self.LAM_H, self.LAM_A)
        self.assertGreater(hhad[2], had[2])
        self.assertLess(hhad[0], had[0])

    def test_hhad_line_is_home_handicap(self):
        """让球线越负（主队让得越多），主胜概率越低。"""
        p0 = mk.market_probs(mk.HHAD(0.0), 1.8, 1.0)[0]
        p1 = mk.market_probs(mk.HHAD(-1.0), 1.8, 1.0)[0]
        p2 = mk.market_probs(mk.HHAD(-2.0), 1.8, 1.0)[0]
        self.assertGreater(p0, p1)
        self.assertGreater(p1, p2)

    def test_btts_monotone(self):
        lo = mk.market_probs(mk.BTTS, 0.5, 0.5)[0]
        hi = mk.market_probs(mk.BTTS, 2.5, 2.5)[0]
        self.assertLess(lo, hi)

    def test_ou_over_increases_with_lambda(self):
        lo = mk.market_probs(mk.OU(2.5), 0.8, 0.8)[0]
        hi = mk.market_probs(mk.OU(2.5), 2.5, 2.5)[0]
        self.assertLess(lo, hi)

    def test_dc_is_union_of_had(self):
        had = mk.market_probs(mk.HAD, self.LAM_H, self.LAM_A)
        dc = mk.market_probs(mk.DC, self.LAM_H, self.LAM_A)
        self.assertAlmostEqual(dc[0], had[0] + had[1], places=10)
        self.assertAlmostEqual(dc[1], had[0] + had[2], places=10)
        self.assertAlmostEqual(dc[2], had[1] + had[2], places=10)

    def test_ah_splits_push_half(self):
        """整数盘口走盘时按各半处理，概率和仍为 1。"""
        vec = mk.market_probs(mk.AH(-1.0), self.LAM_H, self.LAM_A)
        self.assertAlmostEqual(math.fsum(vec), 1.0, places=10)

    def test_subset_prob_matches_vector(self):
        vec = mk.market_probs(mk.HAD, self.LAM_H, self.LAM_A)
        self.assertAlmostEqual(
            mk.subset_prob(mk.HAD, "draw", self.LAM_H, self.LAM_A),
            vec[1], places=12)

    def test_non_partition_rejected_by_market_probs(self):
        spec = mk.MarketSpec(code="LEG", name="单腿",
                             outcomes=("a",),
                             space=mk.ResultSpace.DERIVED_SUBSET)
        with self.assertRaises(ValueError):
            mk.market_probs(spec, 1.0, 1.0)

    def test_unknown_market_raises(self):
        spec = mk.MarketSpec(code="WEIRD", name="怪", outcomes=("a", "b"))
        with self.assertRaises(KeyError):
            mk.market_probs(spec, 1.0, 1.0)

    def test_normalize_false_keeps_raw(self):
        raw = mk.market_probs(mk.HAD, self.LAM_H, self.LAM_A, normalize=False)
        self.assertLess(math.fsum(raw), 1.0)   # 截断损失


class TestExpectedGoals(unittest.TestCase):
    """期望进球估计。"""

    def test_basic_estimate(self):
        lh, la = mk.expected_goals_from_stats(1.8, 1.4, 1.6, 1.5, 2.6)
        self.assertAlmostEqual(lh, 1.8 * 1.5 / 2.6, places=12)
        self.assertAlmostEqual(la, 1.6 * 1.4 / 2.6, places=12)

    def test_shrink_pulls_toward_mean(self):
        lh0, la0 = mk.expected_goals_from_stats(3.0, 1.0, 0.2, 1.0, 2.6)
        lh1, la1 = mk.expected_goals_from_stats(3.0, 1.0, 0.2, 1.0, 2.6,
                                                shrink=0.8)
        self.assertLess(abs(lh1 - 1.3), abs(lh0 - 1.3))
        self.assertGreater(abs(la1 - 1.3), 0.0)

    def test_shrink_one_gives_league_mean(self):
        lh, la = mk.expected_goals_from_stats(3.0, 1.0, 0.2, 1.0, 2.6,
                                             shrink=1.0)
        self.assertAlmostEqual(lh, 1.3, places=9)
        self.assertAlmostEqual(la, 1.3, places=9)

    def test_rejects_negative_input(self):
        with self.assertRaises(ValueError):
            mk.expected_goals_from_stats(-1.0, 1.0, 1.0, 1.0, 2.6)

    def test_rejects_bad_league_avg(self):
        with self.assertRaises(ValueError):
            mk.expected_goals_from_stats(1.0, 1.0, 1.0, 1.0, 0.0)

    def test_rejects_bad_shrink(self):
        with self.assertRaises(ValueError):
            mk.expected_goals_from_stats(1.0, 1.0, 1.0, 1.0, 2.6, shrink=1.5)

    def test_rejects_non_numeric(self):
        # cast 是运行期无操作：既让类型检查器满意（无需 type: ignore，
        # 因为 mypy 与 pyright 对 ignore 是否冗余判定不一致），
        # 又能在运行期真的把字符串传进去验证 TypeError。
        bad = cast(float, "1.0")
        with self.assertRaises(TypeError):
            mk.expected_goals_from_stats(bad, 1.0, 1.0, 1.0, 2.6)

    def test_clamped_to_sane_range(self):
        lh, la = mk.expected_goals_from_stats(50.0, 50.0, 50.0, 50.0, 2.6)
        self.assertLessEqual(lh, 12.0)
        self.assertLessEqual(la, 12.0)


class TestParseCrsKey(unittest.TestCase):
    """上游比分键解析（含歧义拒绝）。"""

    def test_colon_form(self):
        self.assertEqual(mk.parse_crs_key("1:0"), "1:0")
        self.assertEqual(mk.parse_crs_key("2:1"), "2:1")

    def test_zero_padded(self):
        self.assertEqual(mk.parse_crs_key("01:00"), "1:0")

    def test_dash_form(self):
        self.assertEqual(mk.parse_crs_key("1-0"), "1:0")

    def test_four_digit_form(self):
        self.assertEqual(mk.parse_crs_key("0100"), "1:0")
        self.assertEqual(mk.parse_crs_key("0201"), "2:1")

    def test_suffix_stripped(self):
        self.assertEqual(mk.parse_crs_key("1:0|xyz"), "1:0")

    def test_other_buckets(self):
        self.assertEqual(mk.parse_crs_key("胜其他"), "胜其他")
        self.assertEqual(mk.parse_crs_key("otherH"), "胜其他")

    def test_ambiguous_two_digits_rejected(self):
        """两位数字有歧义（10 是 1:0 还是 10:0），必须拒绝而非猜测。"""
        self.assertIsNone(mk.parse_crs_key("10"))

    def test_unlisted_score_rejected(self):
        self.assertIsNone(mk.parse_crs_key("6:0"))

    def test_garbage_rejected(self):
        for bad in ("", "junk", "abc:def", "1:2:3"):
            self.assertIsNone(mk.parse_crs_key(bad))
        self.assertIsNone(mk.parse_crs_key(cast(str, None)))


class TestPoolOddsKeys(unittest.TestCase):
    """poolCode → 键映射。"""

    def test_had_and_hhad_share_keys(self):
        self.assertIs(mk.pool_odds_keys("HAD"), mk.pool_odds_keys("HHAD"))

    def test_hhad_lowercase(self):
        self.assertIs(mk.pool_odds_keys("hhad"), mk.HAD_ODDS_KEYS)

    def test_ttg_keys(self):
        keys = mk.pool_odds_keys("TTG")
        assert keys is not None
        self.assertEqual(keys["s7"], "7+")
        self.assertEqual(keys["s0"], "0")

    def test_hafu_keys(self):
        keys = mk.pool_odds_keys("HAFU")
        assert keys is not None
        self.assertEqual(len(keys), 9)
        self.assertEqual(keys["aa"], "A/A")

    def test_crs_has_no_key_table(self):
        self.assertIsNone(mk.pool_odds_keys("CRS"))

    def test_unknown_pool(self):
        self.assertIsNone(mk.pool_odds_keys("ZZZ"))

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            mk.pool_odds_keys("")


class TestSafeConversions(unittest.TestCase):
    """安全数值转换（防 OverflowError 穿透）。"""

    def test_accepts_numeric_strings(self):
        self.assertAlmostEqual(mk._as_float("2.5", "x"), 2.5)

    def test_rejects_huge_int_without_overflow(self):
        """float(10**400) 抛 OverflowError；必须收敛为 ValueError。"""
        with self.assertRaises(ValueError):
            mk._as_float(10 ** 400, "x")

    def test_rejects_nan_and_inf(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                mk._as_float(bad, "x")

    def test_rejects_none_bool_empty(self):
        for bad in (None, True, False, "", "  ", "abc", [], {}):
            with self.assertRaises(ValueError):
                mk._as_float(bad, "x")

    def test_as_int_truncation_rejected(self):
        with self.assertRaises(ValueError):
            mk._as_int(2.5, "x")

    def test_as_int_ok(self):
        self.assertEqual(mk._as_int("3", "x"), 3)
        self.assertEqual(mk._as_int(3.0, "x"), 3)

    def test_as_int_huge_raises_valueerror(self):
        with self.assertRaises(ValueError):
            mk._as_int(10 ** 400, "x")


class TestResultSpace(unittest.TestCase):
    """结果空间语义。"""

    def test_partition_expected_sum_one(self):
        self.assertEqual(mk.ResultSpace.EXACT_PARTITION.expected_sum, 1.0)
        self.assertTrue(mk.ResultSpace.EXACT_PARTITION.is_partition)

    def test_overlapping_expected_sum_two(self):
        self.assertEqual(mk.ResultSpace.OVERLAPPING.expected_sum, 2.0)
        self.assertFalse(mk.ResultSpace.OVERLAPPING.is_partition)

    def test_subset_expected_sum_is_nan(self):
        self.assertTrue(math.isnan(mk.ResultSpace.DERIVED_SUBSET.expected_sum))

    def test_partition_check_respects_space(self):
        ok, total = mk.partition_check(mk.DC, (0.7, 0.7, 0.6))
        self.assertTrue(ok)
        self.assertAlmostEqual(total, 2.0, places=12)

    def test_partition_check_detects_broken_overlapping(self):
        ok, _ = mk.partition_check(mk.DC, (0.35, 0.35, 0.3))
        self.assertFalse(ok)


class TestCrossMarketMarginals(unittest.TestCase):
    """跨玩法边际一致性校验。"""

    def test_consistent_model_reports_nothing(self):
        per = {sp.code: mk.market_probs(sp, 1.55, 1.20) for sp in mk.catalog()}
        findings = mk.cross_market_marginals(per, tol=0.01)
        self.assertEqual(findings, [])

    def test_broken_had_is_detected(self):
        per = {sp.code: mk.market_probs(sp, 1.55, 1.20) for sp in mk.catalog()}
        per["HAD"] = (0.9, 0.05, 0.05)     # 人为破坏
        findings = mk.cross_market_marginals(per, tol=0.01)
        self.assertTrue(findings)
        # findings 的值类型是 object（泛化字典），需显式转 str 再做子串判断
        checks = [str(f["check"]) for f in findings]
        self.assertTrue(any("HAFU" in c for c in checks), checks)

    def test_tolerance_controls_sensitivity(self):
        per = {sp.code: mk.market_probs(sp, 1.55, 1.20) for sp in mk.catalog()}
        per["HAD"] = (0.46, 0.24, 0.30)
        loose = mk.cross_market_marginals(per, tol=0.5)
        self.assertEqual(loose, [])

    def test_empty_input(self):
        self.assertEqual(mk.cross_market_marginals({}), [])


class TestMarketGroup(unittest.TestCase):
    """盘口组。"""

    def test_group_requires_members(self):
        with self.assertRaises(ValueError):
            mk.MarketGroup(name="x", line=2.5, members=())

    def test_group_line_must_match(self):
        with self.assertRaises(ValueError):
            mk.MarketGroup(name="x", line=2.5, members=(mk.OU(3.5),))

    def test_valid_group(self):
        g = mk.MarketGroup(name="OU2.5", line=2.5, members=(mk.OU(2.5),))
        self.assertEqual(g.n_members, 1)


if __name__ == "__main__":
    unittest.main()
