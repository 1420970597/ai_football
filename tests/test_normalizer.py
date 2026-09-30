#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""collector.normalizer 的单元测试（报告 §3.2/§3.3/§4.4）。

上游真实结构：中国体育彩票官方 API V2 的落盘 JSON。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any, cast

from collector.normalizer import (
    MARKET_1X2,
    MARKET_HANDICAP,
    normalize_matches,
    parse_match,
)

BASE = {
    "基本信息": {
        "场次号": 2001, "场次编号字符串": "周二001",
        "比赛日期": "2025-09-23", "比赛时间": "17:00",
        "联赛名称": "日本职业联赛", "联赛简称": "日职",
        "主队名称": "鹿岛鹿角", "客队名称": "大阪樱花",
        "比赛ID": 2033744, "比赛状态": "Selling", "销售状态": "1",
    },
    "赔率信息": {
        "主胜赔率": "2.13", "平局赔率": "3.45", "客胜赔率": "2.70",
        "让球主胜赔率": "4.50", "让球平局赔率": "3.75",
        "让球客胜赔率": "1.56", "让球盘口": "-1.00",
    },
    "玩法信息": {"可用玩法": [{"玩法代码": "HHAD", "玩法状态": "Selling"}]},
    "数据获取信息": {"获取时间": "2025-09-23T13:42:18.843785",
                     "数据来源": "中国体育彩票官方API V2",
                     "接口版本": "getMatchListV1"},
}


def rec(**over):
    """深拷贝 BASE 并覆盖顶层键。"""
    import copy
    d = copy.deepcopy(BASE)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            d[k].update(v)
        else:
            d[k] = v
    return d


class TestParseMatch(unittest.TestCase):
    def test_parses_both_markets(self):
        snaps = parse_match(rec())
        markets = {s.market for s in snaps}
        self.assertEqual(markets, {MARKET_1X2, MARKET_HANDICAP})

    def test_odds_converted_from_string(self):
        """上游赔率是字符串，必须转 float，否则后续计算报错。"""
        s = [x for x in parse_match(rec()) if x.market == MARKET_1X2][0]
        for o in s.odds:
            self.assertIsInstance(o, float)
        self.assertAlmostEqual(s.odds[0], 2.13, places=6)
        self.assertGreater(s.booksum, 1.0)   # 验证真的能算

    def test_metadata_fields_captured(self):
        s = [x for x in parse_match(rec()) if x.market == MARKET_1X2][0]
        self.assertEqual(s.metadata["比赛日期"], "2025-09-23")
        self.assertEqual(s.metadata["比赛时间"], "17:00")
        self.assertIn("数据来源", s.metadata)

    def test_handicap_line_in_metadata(self):
        s = [x for x in parse_match(rec()) if x.market == MARKET_HANDICAP][0]
        self.assertAlmostEqual(s.metadata["让球盘口"], -1.0, places=6)

    def test_capture_time_parsed(self):
        s = parse_match(rec())[0]
        self.assertEqual(s.captured_at.year, 2025)
        self.assertEqual(s.captured_at.month, 9)
        self.assertIsNotNone(s.captured_at.tzinfo)

    def test_source_override(self):
        s = parse_match(rec(), source="其他来源")[0]
        self.assertEqual(s.source, "其他来源")

    # -- 状态推断 -----------------------------------------------------------

    def test_selling_is_active(self):
        s = parse_match(rec())[0]
        self.assertTrue(s.is_usable())

    def test_suspended_play_state(self):
        r = rec(玩法信息={"可用玩法": [{"玩法代码": "HHAD",
                                       "玩法状态": "Suspended"}]})
        s = parse_match(r)[0]
        self.assertFalse(s.is_usable())

    def test_sales_status_zero_is_delisted(self):
        r = rec(基本信息={"销售状态": "0"})
        s = parse_match(r)[0]
        self.assertFalse(s.is_usable())

    # -- 容错（报告 §3.3 协议漂移） -----------------------------------------

    def test_missing_odds_issues_not_crash(self):
        r = rec(赔率信息={"主胜赔率": "", "平局赔率": "",
                          "客胜赔率": "", "让球主胜赔率": "",
                          "让球平局赔率": "", "让球客胜赔率": ""})
        issues = []
        snaps = parse_match(r, issues=issues)
        self.assertEqual(snaps, [])
        self.assertTrue(len(issues) > 0)

    def test_partial_odds_skips_that_market_only(self):
        """1X2 缺一个赔率 → 跳过 1X2，但让球仍应产出。"""
        r = rec(赔率信息={"客胜赔率": ""})
        issues = []
        snaps = parse_match(r, issues=issues)
        self.assertEqual({s.market for s in snaps}, {MARKET_HANDICAP})
        self.assertTrue(any(i.field == "1X2" for i in issues))

    def test_missing_basic_block(self):
        issues = []
        snaps = parse_match({"赔率信息": BASE["赔率信息"]}, issues=issues)
        self.assertEqual(snaps, [])
        self.assertTrue(len(issues) > 0)

    def test_missing_match_id(self):
        r = rec(基本信息={"场次号": "", "比赛ID": ""})
        issues = []
        self.assertEqual(parse_match(r, issues=issues), [])
        self.assertTrue(any("场次号" in i.field for i in issues))

    def test_missing_team_names(self):
        r = rec(基本信息={"主队名称": "", "主队全称": ""})
        issues = []
        self.assertEqual(parse_match(r, issues=issues), [])

    def test_non_mapping_basic(self):
        issues = []
        self.assertEqual(parse_match({"基本信息": "bad"}, issues=issues), [])
        self.assertTrue(len(issues) > 0)

    def test_invalid_capture_time_falls_back(self):
        r = rec(数据获取信息={"获取时间": "not-a-date"})
        fb = datetime(2020, 1, 1, tzinfo=timezone.utc)
        s = parse_match(r, default_time=fb)[0]
        self.assertEqual(s.captured_at, fb)

    def test_nan_odds_rejected(self):
        r = rec(赔率信息={"主胜赔率": "abc"})
        issues = []
        snaps = parse_match(r, issues=issues)
        self.assertEqual({s.market for s in snaps}, {MARKET_HANDICAP})

    def test_league_falls_back_to_full_name(self):
        r = rec(基本信息={"联赛简称": ""})
        s = parse_match(r)[0]
        self.assertEqual(s.league, "日本职业联赛")


class TestNormalizeMatches(unittest.TestCase):
    def test_batch(self):
        res = normalize_matches([rec(), rec(基本信息={"场次号": 2002})])
        self.assertEqual(res.n_matches, 2)
        self.assertEqual(len(res.snapshots), 4)   # 2 场 × 2 市场
        self.assertEqual(res.n_skipped, 0)

    def test_dedupe_same_record(self):
        """幂等去重（报告 §4.4 完整性维度）。"""
        res = normalize_matches([rec(), rec()], dedupe=True)
        self.assertEqual(len(res.snapshots), 2)   # 去重后只剩 2（1X2 + 让球）

    def test_dedupe_disabled(self):
        res = normalize_matches([rec(), rec()], dedupe=False)
        self.assertEqual(len(res.snapshots), 4)

    def test_skips_non_mapping(self):
        # 故意混入非字典项，验证归一化层跳过而非崩溃
        res = normalize_matches(cast(Any, [rec(), "bad", None]))
        self.assertEqual(res.n_skipped, 2)
        self.assertEqual(len(res.snapshots), 2)

    def test_summary_shape(self):
        res = normalize_matches([rec()])
        s = res.summary()
        for k in ("n_matches", "n_snapshots", "n_issues", "n_skipped", "issues"):
            self.assertIn(k, s)

    def test_empty_input(self):
        res = normalize_matches([])
        self.assertEqual(len(res.snapshots), 0)
        self.assertEqual(res.n_matches, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
