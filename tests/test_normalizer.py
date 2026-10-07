#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""collector.normalizer 的单元测试（报告 §3.2/§3.3/§4.4）。

上游真实结构：中国体育彩票官方 API V2 的落盘 JSON。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
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


class TestLiveBookToSnapshots(unittest.TestCase):
    """内存实时表 → 快照（用户要求：查询读本地，不扫快照库）。

    背景（用户报告问题 2/3）：
      * 决策拿 1.8 小时前的旧价算出「全场进球数>2.5 @3.32」的买入建议，
        而该场比分已 1:3（4 球）——那个盘口早已结算；
      * `/board` 冷启动要扫 6.5 万个快照文件（实测 14~45s）。

    修法：推送（`C105` 周期性全量快照）里本来就有**当前赔率**，
    在 Hub 内存里留一份；查询时直接翻译成快照。
    """

    @staticmethod
    def _tick(mid="m1", chpid="2", hv="2.5", oid="o1", ot="Over",
              odds=1.9, ts=1):
        from collector.leyu_realtime import PriceTick
        return PriceTick(mid=mid, chpid=chpid, hid="h", hv=hv, oid=oid,
                         ot=ot, old_ov=0.0, new_ov=odds, ts_ms=ts)

    def test_ou_maps_to_snapshot(self) -> None:
        from collector.leyu_normalizer import snapshots_from_live
        from collector.leyu_realtime import LiveBook
        b = LiveBook()
        b.upsert_many([self._tick(oid="o1", ot="Over", odds=1.9),
                       self._tick(oid="o2", ot="Under", odds=2.0)])
        out = snapshots_from_live(b.book("m1"), "m1")
        self.assertEqual(len(out), 1)
        s = out[0]
        self.assertEqual(s.market, "OU(2.5)")
        self.assertEqual(s.outcomes, ("over", "under"))
        self.assertAlmostEqual(s.odds[0], 1.9, places=4)
        self.assertEqual(s.metadata["leyu_hv"], "2.5")

    def test_ah_maps_with_signed_line(self) -> None:
        from collector.leyu_normalizer import snapshots_from_live
        from collector.leyu_realtime import LiveBook
        b = LiveBook()
        b.upsert_many([self._tick(chpid="4", hv="-1", oid="h", ot="1",
                                  odds=1.85),
                       self._tick(chpid="4", hv="-1", oid="a", ot="2",
                                  odds=1.95)])
        out = snapshots_from_live(b.book("m1"), "m1")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].market, "AH(-1)")
        self.assertEqual(out[0].outcomes, ("home", "away"))

    def test_half_markets_are_distinct(self) -> None:
        from collector.leyu_normalizer import snapshots_from_live
        from collector.leyu_realtime import LiveBook
        b = LiveBook()
        b.upsert_many([self._tick(chpid="18", hv="1", oid="a", ot="Over",
                                  odds=1.9),
                       self._tick(chpid="18", hv="1", oid="b", ot="Under",
                                  odds=2.0)])
        out = snapshots_from_live(b.book("m1"), "m1")
        self.assertEqual(out[0].market, "OU_1H(1)")

    def test_illegal_odds_are_skipped(self) -> None:
        """<=1.0 的赔率非法，不得进入计算（否则会产出虚假 edge）。"""
        from collector.leyu_normalizer import snapshots_from_live
        from collector.leyu_realtime import LiveBook
        b = LiveBook()
        b.upsert_many([self._tick(oid="a", ot="Over", odds=0.5),
                       self._tick(oid="b", ot="Under", odds=2.0)])
        out = snapshots_from_live(b.book("m1"), "m1")
        self.assertEqual(out, [])       # 缺 over → 整盘口跳过

    def test_unknown_chpid_reported_not_crash(self) -> None:
        from collector.leyu_normalizer import snapshots_from_live
        from collector.leyu_realtime import LiveBook
        b = LiveBook()
        b.upsert_many([self._tick(chpid="999", oid="x", ot="Over")])
        issues: list = []
        out = snapshots_from_live(b.book("m1"), "m1", issues=issues)
        self.assertEqual(out, [])
        self.assertTrue(issues)

    def test_unknown_ot_is_skipped(self) -> None:
        from collector.leyu_normalizer import snapshots_from_live
        from collector.leyu_realtime import LiveBook
        b = LiveBook()
        b.upsert_many([self._tick(oid="x", ot="Weird")])
        issues: list = []
        snapshots_from_live(b.book("m1"), "m1", issues=issues)
        self.assertTrue(issues)


class TestLiveBook(unittest.TestCase):
    """内存实时表 + 本地落盘（用户要求：采集落本地，查询读本地）。"""

    @staticmethod
    def _tick(mid="m1", oid="o1", odds=1.9):
        from collector.leyu_realtime import PriceTick
        return PriceTick(mid=mid, chpid="2", hid="h", hv="2.5", oid=oid,
                         ot="Over", old_ov=0.0, new_ov=odds, ts_ms=1)

    def test_upsert_and_read(self) -> None:
        from collector.leyu_realtime import LiveBook
        b = LiveBook()
        b.upsert_many([self._tick(odds=1.9)])
        self.assertEqual(b.n_matches(), 1)
        self.assertEqual(b.n_rows(), 1)
        self.assertEqual(b.live_mids(), ["m1"])
        self.assertAlmostEqual(b.book("m1")[0].odds, 1.9, places=4)

    def test_latest_value_wins(self) -> None:
        from collector.leyu_realtime import LiveBook
        b = LiveBook()
        b.upsert_many([self._tick(odds=1.9)])
        b.upsert_many([self._tick(odds=1.5)])
        self.assertEqual(b.n_rows(), 1)
        self.assertAlmostEqual(b.book("m1")[0].odds, 1.5, places=4)

    def test_drop_match(self) -> None:
        from collector.leyu_realtime import LiveBook
        b = LiveBook()
        b.upsert_many([self._tick(), self._tick(oid="o2")])
        self.assertEqual(b.drop_match("m1"), 2)
        self.assertEqual(b.book("m1"), [])

    def test_save_and_load_roundtrip(self) -> None:
        """重启后立即有本地数据，无需重扫快照库。"""
        from collector.leyu_realtime import LiveBook
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "live.json")
            b = LiveBook(p)
            b.upsert_many([self._tick(odds=1.9)])
            self.assertTrue(b.save(force=True))
            b2 = LiveBook(p)
            self.assertEqual(b2.load(), 1)
            self.assertAlmostEqual(b2.book("m1")[0].odds, 1.9, places=4)

    def test_load_missing_file_is_safe(self) -> None:
        from collector.leyu_realtime import LiveBook
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(LiveBook(os.path.join(d, "nope.json")).load(), 0)

    def test_load_corrupt_file_is_safe(self) -> None:
        from collector.leyu_realtime import LiveBook
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "live.json")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            b = LiveBook(p)
            self.assertEqual(b.load(), 0)
            self.assertTrue(b.last_error)

    def test_save_throttled_unless_forced(self) -> None:
        from collector.leyu_realtime import LiveBook
        with tempfile.TemporaryDirectory() as d:
            b = LiveBook(os.path.join(d, "live.json"))
            b.upsert_many([self._tick()])
            self.assertTrue(b.save(force=True))
            self.assertFalse(b.save())      # 5s 节流内不重复写

    def test_no_path_means_memory_only(self) -> None:
        from collector.leyu_realtime import LiveBook
        b = LiveBook(None)
        b.upsert_many([self._tick()])
        self.assertFalse(b.save(force=True))
        self.assertEqual(b.load(), 0)


class TestLiveBookQuoteAge(unittest.TestCase):
    """回归：回填的行情必须带**真实年龄**，不得冒充「正在进行中」。

    历史缺陷（本项目真实故障）：`load()` 把每行的 `at` 写成「载入这一刻」，
    于是 24 小时前的旧行情被 `/api/v1/analysis` 报成
    `count=527, source=push`（自称「真实进行中」），而当时推送链路
    一条消息都没收到（`connected=0, messages=0`）。这与 HANDOVER §6.3
    的「用快照时效冒充进行中」是同一类错误，只是换了持久化回填这条路径
    重新长出来。

    本组用例锁死三件事：
      1. 新鲜行情 → 算活跃（正常路径）；
      2. 陈旧/无时间戳行情 → **不得**算活跃（边界路径）；
      3. 不带门禁的 `live_mids()` 保持旧行为（向后兼容）。
    """

    @staticmethod
    def _write_live(path: str, ts_ms_list: Any) -> None:
        """按 `save()` 的落盘格式手写一份 live.json。"""
        rows = [[f"m{i}", "2", "2.5", "o1", "Over", 1.9, ts]
                for i, ts in enumerate(ts_ms_list)]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "rows": rows}, fh)

    def test_fresh_quotes_count_as_live(self) -> None:
        """正常路径：刚推送的行情算活跃。"""
        from collector.leyu_realtime import DEFAULT_QUOTE_MAX_AGE_S, LiveBook
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "live.json")
            self._write_live(p, [int(time.time() * 1000) - 5_000])
            b = LiveBook(p)
            self.assertEqual(b.load(), 1)
            self.assertEqual(
                b.live_mids(max_age_s=DEFAULT_QUOTE_MAX_AGE_S), ["m0"])

    def test_stale_quotes_are_not_live(self) -> None:
        """边界路径：24 小时前的行情**不得**算作进行中。"""
        from collector.leyu_realtime import DEFAULT_QUOTE_MAX_AGE_S, LiveBook
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "live.json")
            self._write_live(p, [int(time.time() * 1000) - 24 * 3_600_000])
            b = LiveBook(p)
            self.assertEqual(b.load(), 1)
            self.assertEqual(b.live_mids(max_age_s=DEFAULT_QUOTE_MAX_AGE_S), [])
            # 回填仍然可用（重启即有数据），只是不再冒充新鲜
            self.assertEqual(b.n_matches(), 1)

    def test_age_s_is_truthful_after_load(self) -> None:
        """`age_s` 必须反映真实年龄，而不是「刚刚载入」。"""
        from collector.leyu_realtime import LiveBook
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "live.json")
            self._write_live(p, [int(time.time() * 1000) - 7_200_000])
            b = LiveBook(p)
            b.load()
            age = b.book("m0")[0].age_s
            self.assertGreater(age, 7_100, "应约为 7200s，而不是 ~0s")
            self.assertAlmostEqual(b.book("m0")[0].quote_age_s, 7_200,
                                   delta=60)

    def test_missing_timestamp_is_never_fresh(self) -> None:
        """边界路径：`ts_ms` 缺失时无从证明新鲜 → 一律不算活跃。"""
        from collector.leyu_realtime import DEFAULT_QUOTE_MAX_AGE_S, LiveBook
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "live.json")
            self._write_live(p, [0])
            b = LiveBook(p)
            self.assertEqual(b.load(), 1)
            self.assertEqual(b.book("m0")[0].quote_age_s, float("inf"))
            self.assertEqual(b.live_mids(max_age_s=DEFAULT_QUOTE_MAX_AGE_S), [])

    def test_live_mids_without_limit_keeps_backward_compat(self) -> None:
        """不传门禁时保持旧行为（避免破坏既有调用方）。"""
        from collector.leyu_realtime import LiveBook
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "live.json")
            self._write_live(p, [int(time.time() * 1000) - 24 * 3_600_000])
            b = LiveBook(p)
            b.load()
            self.assertEqual(b.live_mids(), ["m0"])

    def test_health_exposes_true_age_and_fresh_count(self) -> None:
        """健康检查必须能看出「数据其实是旧的」（可观测性）。"""
        from collector.leyu_realtime import LiveBook
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "live.json")
            self._write_live(p, [int(time.time() * 1000) - 24 * 3_600_000])
            b = LiveBook(p)
            b.load()
            h = b.health()
            self.assertEqual(h["fresh_rows"], 0)
            self.assertGreater(h["quote_newest_age_s"], 86_000)

    def test_health_stays_valid_json_when_age_unjudgeable(self) -> None:
        """边界路径：全部行情都无时间戳时，健康结果仍必须是**合法 JSON**。

        为何关键：`allow_nan=False` 下 `Infinity` 不是合法 JSON，
        浏览器 `JSON.parse` 会直接报错。而“无时间戳”是真实可能的
        （落盘兼容/脏字段）。
        """
        from collector.leyu_realtime import LiveBook
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "live.json")
            self._write_live(p, [0])
            b = LiveBook(p)
            b.load()
            h = b.health()
            self.assertIsNone(h["quote_newest_age_s"])
            self.assertEqual(h["fresh_rows"], 0)
            json.dumps(h, allow_nan=False)   # 不抛异常即为通过

    def test_upsert_after_load_restores_freshness(self) -> None:
        """新推送到来后该场立即恢复活跃（门禁不阻碍正常采集）。"""
        from collector.leyu_realtime import DEFAULT_QUOTE_MAX_AGE_S, LiveBook, PriceTick
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "live.json")
            self._write_live(p, [int(time.time() * 1000) - 24 * 3_600_000])
            b = LiveBook(p)
            b.load()
            self.assertEqual(b.live_mids(max_age_s=DEFAULT_QUOTE_MAX_AGE_S), [])
            b.upsert_many([PriceTick(
                mid="m0", chpid="2", hid="h", hv="2.5", oid="o1",
                ot="Over", old_ov=1.9, new_ov=1.95,
                ts_ms=int(time.time() * 1000))])
            self.assertEqual(
                b.live_mids(max_age_s=DEFAULT_QUOTE_MAX_AGE_S), ["m0"])


class TestSnapshotsFromLiveCarriesNames(unittest.TestCase):
    """回归：实时表**不带队名**，必须由调用方补上。

    为何关键：买入建议的中文标签需要队名（「曼联上半场-1」，
    用户明确要求与乐鱼一致）。若实时表转换时丢了队名，
    `decide_match` 从 `snaps[0]` 取到的 home/away 就是空串，
    标签会静默退化成「主队/客队」—— 功能受损但**不报错**，
    属于最难发现的那类缺陷。
    """

    @staticmethod
    def _book():
        from collector.leyu_realtime import LiveBook, PriceTick
        b = LiveBook()
        b.upsert_many([
            PriceTick(mid="m1", chpid="4", hid="h", hv="-1", oid="o1",
                      ot="1", old_ov=0.0, new_ov=1.85, ts_ms=1),
            PriceTick(mid="m1", chpid="4", hid="h", hv="-1", oid="o2",
                      ot="2", old_ov=0.0, new_ov=1.95, ts_ms=1),
        ])
        return b

    def test_names_are_carried_through(self) -> None:
        from collector.leyu_normalizer import snapshots_from_live
        out = snapshots_from_live(self._book().book("m1"), "m1",
                                  home="曼联", away="利物浦", league="英超")
        self.assertEqual(len(out), 1)
        s = out[0]
        self.assertEqual(s.home, "曼联")
        self.assertEqual(s.away, "利物浦")
        self.assertEqual(s.league, "英超")

    def test_names_default_to_empty(self) -> None:
        from collector.leyu_normalizer import snapshots_from_live
        out = snapshots_from_live(self._book().book("m1"), "m1")
        self.assertEqual(out[0].home, "")
        self.assertEqual(out[0].league, "")

    def test_label_uses_team_name_end_to_end(self) -> None:
        """端到端：队名 → 乐鱼风格中文标签（用户要求的形式）。"""
        from collector.leyu_normalizer import snapshots_from_live
        from core.market_labels import format_market
        s = snapshots_from_live(self._book().book("m1"), "m1",
                                home="曼联", away="利物浦", league="英超")[0]
        line = str((s.metadata or {}).get("leyu_hv") or "")
        self.assertEqual(
            format_market(s.market, "home", line, home=s.home, away=s.away),
            "曼联全场-1")
        self.assertEqual(
            format_market(s.market, "away", line, home=s.home, away=s.away),
            "利物浦全场+1")


class TestLiveBookToleratesDirtyPayload(unittest.TestCase):
    """回归：脏赔率不得让推送批次中斷（**热路径崩溃风险**）。

    为何严重：`upsert_many` 跑在推送热路径上（`C105` 每小时可达 16 万条），
    载荷来自上游、不可信。早期直接 `float(t.new_ov)`，遇到非数值就抛
    `ValueError` → 冒到推送循环 → 被外层 `except` 当成链路故障
    → **触发不必要的重连**（丢消息）。宁可丢一个脏值，不可断整条链路。
    """

    @staticmethod
    def _tick(oid: str, ov: Any) -> Any:
        from collector.leyu_realtime import PriceTick
        return PriceTick(mid="m1", chpid="2", hid="h", hv="2.5", oid=oid,
                         ot="Under", old_ov=0.0, new_ov=ov, ts_ms=1)

    def test_dirty_values_are_dropped_not_raised(self) -> None:
        from collector.leyu_realtime import LiveBook
        b = LiveBook()
        b.upsert_many([self._tick("a", "abc"), self._tick("b", None),
                       self._tick("c", 0.5), self._tick("d", 2.0)])
        # 只有合法且 >1.0 的那条留下
        self.assertEqual(b.n_rows(), 1)
        self.assertEqual(b.updates, 1)
        self.assertAlmostEqual(b.book("m1")[0].odds, 2.0, places=4)

    def test_nan_and_inf_are_dropped(self) -> None:
        from collector.leyu_realtime import LiveBook
        b = LiveBook()
        b.upsert_many([self._tick("a", float("nan")),
                       self._tick("b", float("inf")),
                       self._tick("c", 1.95)])
        self.assertEqual(b.n_rows(), 1)
        self.assertAlmostEqual(b.book("m1")[0].odds, 1.95, places=4)

    def test_valid_batch_is_fully_stored(self) -> None:
        from collector.leyu_realtime import LiveBook
        b = LiveBook()
        b.upsert_many([self._tick("a", 1.9), self._tick("b", 2.1)])
        self.assertEqual(b.n_rows(), 2)

    def test_save_tolerates_dirty_fields(self) -> None:
        """落盘路径同样不得因脏字段抛异常（否则保存整批失败）。"""
        import os
        import tempfile

        from collector.leyu_realtime import LiveBook
        with tempfile.TemporaryDirectory() as d:
            b = LiveBook(os.path.join(d, "live.json"))
            b.upsert_many([self._tick("a", 1.9)])
            # 手工注入脏值，模拟反序列化/测试替身带来的异常数据
            row = b._rows[(("m1"), "2", "2.5", "a")]
            object.__setattr__(row, "odds", "bad") if hasattr(row, "__dict__") else None
            self.assertTrue(b.save(force=True), "脏字段不应让落盘失败")


class TestScoreStore(unittest.TestCase):
    """**根因回归**：赛果必须落盘，否则结算永远拿不到比分（用户问题 2）。

    实测两条上游路径都拿不到**历史**比分：
      * `getOriginalDataPB`（赛程）不返回 `msc`；
      * `structureMatchBaseInfoByMidsPB`（盘口）对**已结束**场次返回空串
        （实测 11/11 全空）。
    即赛果只在「进行中」那个时间窗内可得，而结算总在结束**之后** →
    `graded=0` → 命中率/ROI/CLV 永远算不出来。

    修法：进程在收到推送时（`C103` 比分 / `C109` 结束）就落盘。
    """

    def _store(self, d: str) -> Any:
        from collector.leyu_realtime import ScoreStore
        return ScoreStore(os.path.join(d, "scores.json"))

    def test_save_and_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            st = self._store(d)
            self.assertTrue(st.save({"m1": (2, 1), "m2": (0, 0)}, {"m1"}))
            got = st.load()
            self.assertEqual(got["m1"]["ft"], [2, 1])
            self.assertTrue(got["m1"]["done"], "已结束必须标记 done")
            self.assertFalse(got["m2"]["done"], "未完赛不得标记 done")

    def test_done_flag_is_critical_for_safety(self) -> None:
        """`done` 是安全红线：拿“进行中”的比分结算会把还在踢的算成已定输赢。"""
        with tempfile.TemporaryDirectory() as d:
            st = self._store(d)
            st.save({"live": (1, 0)}, set())          # 未结束
            self.assertFalse(st.load()["live"]["done"])

    def test_dirty_scores_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            st = self._store(d)
            self.assertTrue(st.save(
                {"ok": (1, 2), "bad": (None, None)}, {"ok"}))
            got = st.load()
            self.assertIn("ok", got)
            self.assertNotIn("bad", got, "脏比分不得写入（宁缺勿错）")

    def test_missing_file_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self._store(d).load(), {})

    def test_corrupt_file_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "scores.json")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            st = self._store(d)
            self.assertEqual(st.load(), {})
            self.assertTrue(st.last_error)

    def test_no_path_is_memory_only(self) -> None:
        from collector.leyu_realtime import ScoreStore
        st = ScoreStore(None)
        self.assertFalse(st.save({"m1": (1, 0)}, {"m1"}))
        self.assertEqual(st.load(), {})

    def test_save_throttled_unless_forced(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            st = self._store(d)
            self.assertTrue(st.save({"m1": (1, 0)}, {"m1"}, force=True))
            self.assertFalse(st.save({"m1": (1, 0)}, {"m1"}))


class TestHubPersistsScoresForSettlement(unittest.TestCase):
    """Hub 必须把推送到的比分/结束状态落盘并能重启回填。"""

    def test_c109_marks_finished_and_saves(self) -> None:
        from collector.leyu_realtime import RealtimeHub
        with tempfile.TemporaryDirectory() as d:
            hub = RealtimeHub(session_provider=None,
                              trend_root=os.path.join(d, "_trends"))
            # C103 比分推送
            import base64
            import gzip as _gz
            cd103 = base64.b64encode(_gz.compress(json.dumps(
                {"mid": "m1", "msc": ["S0|1:0", "S1|2:1"],
                 "mst": "90"}).encode()
            )).decode()
            hub._handle_message({"cmd": "C103", "cd": cd103})
            self.assertEqual(hub.score("m1"), (2, 1))
            # C109 结束通知
            cd109 = base64.b64encode(_gz.compress(json.dumps(
                [{"mid": "m1", "ms": 110}]).encode())).decode()
            hub._handle_message({"cmd": "C109", "cd": cd109})
            self.assertTrue(hub.is_finished("m1"))
            # 重启（新实例）应能从磁盘回填
            hub2 = RealtimeHub(session_provider=None,
                               trend_root=os.path.join(d, "_trends"))
            self.assertEqual(hub2.score("m1"), (2, 1))
            self.assertTrue(hub2.is_finished("m1"))
            self.assertIn("m1", hub2.finished_mids())
            self.assertEqual(hub2.scores_snapshot().get("m1"), (2, 1))
