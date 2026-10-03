#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘口中文标签的单元测试。

用户要求：决策结果要说人话 —— 「上半场大1.5」，而不是 `OU_1H(1.5) over`。

    python3 -m unittest tests.test_market_labels -v
"""

from __future__ import annotations

import unittest

from core.market_labels import (
    FAMILY_NAMES,
    TOTAL_OUTCOME_ZH,
    WINNER_OUTCOME_ZH,
    ah_side_label,
    describe_market,
    format_market,
    format_pick,
    parse_market_code,
)


class TestParseMarketCode(unittest.TestCase):
    def test_full_and_half(self) -> None:
        self.assertEqual(parse_market_code("OU_1H(1.5)"), ("OU", True, "1.5"))
        self.assertEqual(parse_market_code("OU(2.5)"), ("OU", False, "2.5"))
        self.assertEqual(parse_market_code("HAD"), ("HAD", False, ""))
        self.assertEqual(parse_market_code("HAD_1H"), ("HAD", True, ""))
        self.assertEqual(parse_market_code("AH(-0.5)"), ("AH", False, "-0.5"))

    def test_case_insensitive(self) -> None:
        self.assertEqual(parse_market_code("ou_1h(1.5)"), ("OU", True, "1.5"))

    def test_unknown_returns_none(self) -> None:
        for bad in ("WEIRD(9)", "", None, 123, "OU_2H(1)"):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_market_code(bad))


class TestFormatMarket(unittest.TestCase):
    """核心：用户要的「上半场大1.5」这类描述。"""

    def test_user_example(self) -> None:
        self.assertEqual(format_market("OU_1H(1.5)", "over", "1.5"),
                         "上半场大1.5")

    def test_ou_full(self) -> None:
        self.assertEqual(format_market("OU(2.5)", "under"), "全场小2.5")
        self.assertEqual(format_market("OU(2.75)", "over"), "全场大2.75")

    def test_ou_first_half(self) -> None:
        self.assertEqual(format_market("OU_1H(0.5)", "under"), "上半场小0.5")
        self.assertEqual(format_market("OU_1H(1.25)", "over"), "上半场大1.25")

    def test_had(self) -> None:
        self.assertEqual(format_market("HAD", "home"), "全场主胜")
        self.assertEqual(format_market("HAD", "draw"), "全场平局")
        self.assertEqual(format_market("HAD", "away"), "全场客胜")
        self.assertEqual(format_market("HAD_1H", "home"), "上半场主胜")

    def test_ah_uses_raw_line_when_given(self) -> None:
        """给出原始线值时优先用它：代码里的 0.25 只是 0/0.5 的中点近似。"""
        self.assertEqual(format_market("AH_1H(0.25)", "home", "0/0.5"),
                         "上半场主队受让0/0.5")

    def test_ah_falls_back_to_code_line(self) -> None:
        self.assertEqual(format_market("AH_1H(0.5)", "away"),
                         "上半场客队让0.5")

    def test_unknown_market_falls_back_not_raise(self) -> None:
        """展示层不能因为陌生盘口就崩掉。"""
        out = format_market("WEIRD(9)", "x")
        self.assertIn("WEIRD", out)

    def test_format_pick_is_alias(self) -> None:
        self.assertEqual(format_pick("OU_1H(1.5)", "over", "1.5"),
                         format_market("OU_1H(1.5)", "over", "1.5"))


class TestAhSideLabel(unittest.TestCase):
    """让球方向必须正确：line 是**主队让球线**（有符号）。"""

    def test_home_gives_when_line_negative(self) -> None:
        # 主队让球 → 押主队是「主队让」，押客队是「客队受让」
        self.assertEqual(ah_side_label("home", "-0.5"), "主队让0.5")
        self.assertEqual(ah_side_label("away", "-0.5"), "客队受让0.5")

    def test_home_receives_when_line_positive(self) -> None:
        # 主队受让 → 押主队是「主队受让」，押客队是「客队让」
        self.assertEqual(ah_side_label("home", "0.5"), "主队受让0.5")
        self.assertEqual(ah_side_label("away", "0.5"), "客队让0.5")

    def test_pk_line_has_no_direction(self) -> None:
        self.assertEqual(ah_side_label("home", "0"), "主队平手")
        self.assertEqual(ah_side_label("away", "0"), "客队平手")

    def test_composite_line_uses_first_segment_for_sign(self) -> None:
        self.assertEqual(ah_side_label("home", "-0.5/1"), "主队让0.5/1")
        self.assertEqual(ah_side_label("away", "-0.5/1"), "客队受让0.5/1")

    def test_never_shows_double_negation(self) -> None:
        """不能出现「主队让-0.5」这种双重否定。"""
        for line in ("-0.5", "-1", "-1.5", "-0.5/1"):
            for oc in ("home", "away"):
                with self.subTest(line=line, oc=oc):
                    self.assertNotIn("-", ah_side_label(oc, line))

    def test_unknown_outcome_is_tolerated(self) -> None:
        self.assertIn("让球", ah_side_label("weird", "-0.5"))

    def test_empty_line(self) -> None:
        self.assertEqual(ah_side_label("home", ""), "主队让球")


class TestDescribeMarket(unittest.TestCase):
    def test_describes_without_outcome(self) -> None:
        self.assertEqual(describe_market("OU_1H(1.5)"), "上半场大小 1.5")
        self.assertEqual(describe_market("AH(0.25)"), "全场让球 0.25")
        self.assertEqual(describe_market("HAD_1H"), "上半场独赢")

    def test_composite_zero_line_is_not_pk(self) -> None:
        """`0/0.5` 首段是 0，但整体有方向，不能当成平手盘。"""
        self.assertNotIn("平手", ah_side_label("home", "0/0.5"))
        self.assertIn("受让", ah_side_label("home", "0/0.5"))
        self.assertIn("让", ah_side_label("away", "0/0.5"))

    def test_unknown_passthrough(self) -> None:
        self.assertEqual(describe_market("WEIRD"), "WEIRD")


class TestExports(unittest.TestCase):
    def test_all_exports_exist(self) -> None:
        import core.market_labels as mod
        missing = [n for n in mod.__all__ if not hasattr(mod, n)]
        self.assertEqual(missing, [], "__all__ 声明了未定义的名字")

    def test_star_import_works(self) -> None:
        ns: dict = {}
        exec("from core.market_labels import *", ns)  # noqa: S102 - 验证导出完整性
        self.assertIn("format_market", ns)

    def test_label_maps_are_consistent(self) -> None:
        self.assertEqual(WINNER_OUTCOME_ZH["home"], "主胜")
        self.assertEqual(TOTAL_OUTCOME_ZH["over"], "大")
        self.assertEqual(FAMILY_NAMES["OU"], "大小")


if __name__ == "__main__":
    unittest.main(verbosity=2)
