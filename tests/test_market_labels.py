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
    format_raw_market,
    normalize_market_name,
    normalize_outcome_label,
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
    """核心：用户要的**乐鱼风格**中文描述。

    用户原话：「要把盘口信息与买入建议以中文形式展示，与 leyu 一致，
    如：xx队上半场-1、上半场进球数>1/1.5」。

    两个约定：
      * 让球 → `<队名><半场><带符号线值>`（如「曼联上半场-1」）
      * 大小 → `<半场>进球数<比较符><线值>`（如「上半场进球数>1/1.5」）
    """

    def test_user_example(self) -> None:
        self.assertEqual(format_market("OU_1H(1.5)", "over", "1.5"),
                         "上半场进球数>1.5")

    def test_user_example_ah_with_team(self) -> None:
        """用户示例一：「xx队上半场-1」。"""
        self.assertEqual(
            format_market("AH_1H(-1)", "home", "-1", "曼联", "利物浦"),
            "曼联上半场-1")

    def test_user_example_ou_compound_line(self) -> None:
        """用户示例二：「上半场进球数>1/1.5」（复合盘原样保留）。"""
        self.assertEqual(format_market("OU_1H(1.25)", "over", "1/1.5"),
                         "上半场进球数>1/1.5")

    def test_ou_full(self) -> None:
        self.assertEqual(format_market("OU(2.5)", "under"),
                         "全场进球数<2.5")
        self.assertEqual(format_market("OU(2.75)", "over"),
                         "全场进球数>2.75")

    def test_ou_first_half(self) -> None:
        self.assertEqual(format_market("OU_1H(0.5)", "under"),
                         "上半场进球数<0.5")
        self.assertEqual(format_market("OU_1H(1.25)", "over"),
                         "上半场进球数>1.25")

    def test_had(self) -> None:
        self.assertEqual(format_market("HAD", "home"), "全场主胜")
        self.assertEqual(format_market("HAD", "draw"), "全场平局")
        self.assertEqual(format_market("HAD", "away"), "全场客胜")
        self.assertEqual(format_market("HAD_1H", "home"), "上半场主胜")

    def test_ah_uses_raw_line_when_given(self) -> None:
        """给出原始线值时优先用它：代码里的 0.25 只是 0/0.5 的中点近似。"""
        self.assertEqual(
            format_market("AH_1H(0.25)", "home", "0/0.5", "曼联", "利物浦"),
            "曼联上半场+0/0.5")

    def test_ah_away_side_negates_line(self) -> None:
        """客队那一侧必须**取反**，否则方向错反（比不展示更危险）。"""
        self.assertEqual(
            format_market("AH_1H(-1)", "away", "-1", "曼联", "利物浦"),
            "利物浦上半场+1")
        self.assertEqual(
            format_market("AH_1H(0.5)", "away", "0.5", "曼联", "利物浦"),
            "利物浦上半场-0.5")

    def test_ah_falls_back_to_code_line(self) -> None:
        self.assertEqual(format_market("AH_1H(-0.5)", "home", home="曼联"),
                         "曼联上半场-0.5")

    def test_ah_without_team_names_uses_placeholder(self) -> None:
        """未提供队名时回退为「主队/客队」，不得报错。"""
        self.assertEqual(format_market("AH(-1)", "home", "-1"),
                         "主队全场-1")
        self.assertEqual(format_market("AH(-1)", "away", "-1"),
                         "客队全场+1")

    def test_ah_zero_line_has_no_sign(self) -> None:
        """平手盘不加符号（`+0`/`-0` 都会让人困惑）。"""
        self.assertEqual(format_market("AH(0)", "home", "0", home="曼联"),
                         "曼联全场0")

    def test_unknown_market_falls_back_not_raise(self) -> None:
        """展示层不能因为陌生盘口就崩掉。"""
        out = format_market("WEIRD(9)", "x")
        self.assertIn("WEIRD", out)

    def test_english_upstream_fields_are_localized(self) -> None:
        self.assertEqual(normalize_outcome_label("Over"), "大")
        self.assertEqual(normalize_outcome_label("away"), "客队")
        self.assertEqual(normalize_market_name("Both Teams To Score", "RAW_998"), "其他玩法（RAW_998）")
        self.assertEqual(format_raw_market("RAW_998", "yes", market_name="双方进球"), "双方进球是")

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


class TestNegateLine(unittest.TestCase):
    """客队那一侧必须取反 —— 方向错反比不展示更危险。"""

    def test_simple_flip(self) -> None:
        from core.market_labels import negate_line
        self.assertEqual(negate_line("-1"), "1")
        self.assertEqual(negate_line("1"), "-1")
        self.assertEqual(negate_line("-0.5"), "0.5")

    def test_compound_flips_whole_sign_only(self) -> None:
        """复合盘整体翻符号，不逐段处理（逐段会写出 `0/-0.5` 这种脏写法）。"""
        from core.market_labels import negate_line
        self.assertEqual(negate_line("0/0.5"), "-0/0.5")
        self.assertEqual(negate_line("-0/0.5"), "0/0.5")
        self.assertEqual(negate_line("1/1.5"), "-1/1.5")

    def test_zero_stays_zero(self) -> None:
        """平手盘取反仍是平手，不能写成 `-0`。"""
        from core.market_labels import negate_line
        for z in ("0", "0/0", "+0"):
            self.assertEqual(negate_line(z), "0")

    def test_empty_is_empty(self) -> None:
        from core.market_labels import negate_line
        self.assertEqual(negate_line(""), "")
        self.assertEqual(negate_line(None), "")


class TestFormatSelectionAlias(unittest.TestCase):
    """`format_selection` 与 `format_market` 必须给出同一结果（同一套措辞）。"""

    def test_same_as_format_market(self) -> None:
        from core.market_labels import format_selection
        for args in (("AH_1H(-1)", "home", "-1", "曼联", "利物浦"),
                     ("OU_1H(1.25)", "over", "1/1.5", "", ""),
                     ("HAD", "draw", "", "", "")):
            mk, oc, ln, h, a = args
            self.assertEqual(
                format_selection(mk, oc, ln, home=h, away=a),
                format_market(mk, oc, ln, home=h, away=a))


class TestLlmLabelConsistency(unittest.TestCase):
    """**关键回归**：提示词里的中文标签必须能被解析器原样找回。

    历史故障：提示词展示了中文标签，而解析器只认内部代码，
    LLM 一用中文作答就**整行被静默丢弃** → 「跑完却零买入建议」。
    现在标签带上了队名（`曼联上半场-1`），若解析器不传队名就会再次错配。
    """

    def _lookup(self, **kw):
        from service.match_decision import _lookup_probability
        return _lookup_probability(**kw)

    def test_prompt_label_roundtrips_with_team_names(self) -> None:
        mkt, oc, ln = "AH_1H(-1)", "home", "-1"
        label = format_market(mkt, oc, ln, home="曼联", away="利物浦")
        self.assertEqual(label, "曼联上半场-1")
        got = self._lookup(raw={label: 0.62}, outcome=oc, market=mkt, line=ln,
                           home="曼联", away="利物浦")
        self.assertAlmostEqual(got or 0.0, 0.62, places=6)

    def test_lookup_accepts_label_without_team_names(self) -> None:
        """LLM 省略队名时也要兼容（不能因格式差异丢盘）。"""
        mkt, oc, ln = "AH_1H(-1)", "home", "-1"
        plain = format_market(mkt, oc, ln)          # 主队上半场-1
        got = self._lookup(raw={plain: 0.5}, outcome=oc, market=mkt, line=ln,
                           home="曼联", away="利物浦")
        self.assertAlmostEqual(got or 0.0, 0.5, places=6)

    def test_lookup_accepts_internal_code(self) -> None:
        got = self._lookup(raw={"home": 0.7}, outcome="home",
                           market="AH_1H(-1)", line="-1")
        self.assertAlmostEqual(got or 0.0, 0.7, places=6)

    def test_lookup_ou_label_roundtrips(self) -> None:
        mkt, oc, ln = "OU_1H(1.25)", "over", "1/1.5"
        label = format_market(mkt, oc, ln)
        self.assertEqual(label, "上半场进球数>1/1.5")
        got = self._lookup(raw={label: 0.4}, outcome=oc, market=mkt, line=ln)
        self.assertAlmostEqual(got or 0.0, 0.4, places=6)

    def test_boolean_probability_rejected(self) -> None:
        """`True` 是 int 子类，不能当概率（否则 bool 会被静默当 1.0）。"""
        got = self._lookup(raw={"home": True}, outcome="home",
                           market="AH(0)", line="0")
        self.assertIsNone(got)

    def test_missing_returns_none(self) -> None:
        got = self._lookup(raw={"away": 0.5}, outcome="home",
                           market="AH(0)", line="0")
        self.assertIsNone(got)
