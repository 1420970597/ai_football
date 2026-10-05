#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""投注结算与决策台账的单元测试（T11）。

校验用户要求：「记录算法决策结果与比赛实际结果，做本地统计计算」
（用于验证 LLM 判定准确性）。

关键回归点：
  1. **复合盘**（`0/0.5`）必须拆成两个半注 —— 只取中点会算错一半的注单
  2. 走水 / 赢半 / 输半必须与「赢 / 输」区分（混为一谈会系统性高估）
  3. 上半场盘口**缺半场比分时必须 void**，不得用全场比分硬算
  4. 命中率/ROI 的**分母只含真正下注的行** —— 台账同时记录被门控
     拦截的盘口，若把它们也算成本金，ROI 会被稀释成接近 0
  5. CLV = 收盘价/买入价 - 1（正 = 买得便宜）
  6. 台账落盘/重读一致，且同盘口多次决策取**最新一条**

    python3 -m unittest tests.test_settlement -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest import mock

from core.settlement import (
    SETTLE_HALF_LOST,
    SETTLE_HALF_WON,
    SETTLE_LOST,
    SETTLE_PENDING,
    SETTLE_PUSH,
    SETTLE_VOID,
    SETTLE_WON,
    clv,
    pnl_for,
    settle_pick,
    split_line,
    summarise,
)
from service.ledger import DecisionLedger, LedgerEntry


class TestSplitLine(unittest.TestCase):
    """线值拆分：复合盘是真实数据里最常见的写法。"""

    def test_simple_line(self) -> None:
        self.assertEqual(split_line("2.5"), [2.5])
        self.assertEqual(split_line("0"), [0.0])

    def test_composite_line(self) -> None:
        """`0/0.5` 必须拆成两个半注（这是乐鱼 hv 的真实写法）。"""
        self.assertEqual(split_line("0/0.5"), [0.0, 0.5])
        self.assertEqual(split_line("2/2.5"), [2.0, 2.5])

    def test_negative_line(self) -> None:
        self.assertEqual(split_line("-0.5"), [-0.5])
        self.assertEqual(split_line("-1/-0.5"), [-1.0, -0.5])

    def test_garbage_returns_empty(self) -> None:
        """无法解析 → 空列表（调用方据此判 void，而不是猜）。"""
        self.assertEqual(split_line(""), [])
        self.assertEqual(split_line("abc"), [])
        self.assertEqual(split_line("1/"), [])


class TestHADSettlement(unittest.TestCase):
    """独赢盘：只有赢/输，没有走水。"""

    def test_home_win(self) -> None:
        st, _ = settle_pick("HAD", "home", "", (2, 1))
        self.assertEqual(st, SETTLE_WON)

    def test_draw(self) -> None:
        st, _ = settle_pick("HAD", "draw", "", (1, 1))
        self.assertEqual(st, SETTLE_WON)

    def test_away_win_loses_home_bet(self) -> None:
        st, _ = settle_pick("HAD", "home", "", (0, 3))
        self.assertEqual(st, SETTLE_LOST)

    def test_chinese_outcome_names(self) -> None:
        """兼容中文结果名（台账可能来自中文提示词链路）。"""
        self.assertEqual(settle_pick("HAD", "主胜", "", (1, 0))[0], SETTLE_WON)
        self.assertEqual(settle_pick("HAD", "平局", "", (1, 1))[0], SETTLE_WON)
        self.assertEqual(settle_pick("HAD", "客胜", "", (0, 1))[0], SETTLE_WON)

    def test_leyu_raw_ot_values(self) -> None:
        """乐鱼原始 ot 取值 1/X/2 必须能识别。"""
        self.assertEqual(settle_pick("HAD", "1", "", (1, 0))[0], SETTLE_WON)
        self.assertEqual(settle_pick("HAD", "X", "", (1, 1))[0], SETTLE_WON)
        self.assertEqual(settle_pick("HAD", "2", "", (0, 1))[0], SETTLE_WON)

    def test_half_time_had_uses_half_score(self) -> None:
        """上半场独赢必须用半场比分（用全场会算反）。"""
        # 半场 0:1（客队领先）但全场 3:1（主队赢）
        st, _ = settle_pick("HAD_1H", "away", "", (3, 1), (0, 1))
        self.assertEqual(st, SETTLE_WON)
        st2, _ = settle_pick("HAD_1H", "home", "", (3, 1), (0, 1))
        self.assertEqual(st2, SETTLE_LOST)


class TestOUSettlement(unittest.TestCase):
    """大小球：含走水与复合盘。"""

    def test_over_wins(self) -> None:
        self.assertEqual(settle_pick("OU(2.5)", "over", "", (2, 1))[0],
                         SETTLE_WON)
        self.assertEqual(settle_pick("OU(2.5)", "under", "", (2, 1))[0],
                         SETTLE_LOST)

    def test_integer_line_push(self) -> None:
        """整数盘打平 → 走水（退回本金），不是输。"""
        self.assertEqual(settle_pick("OU(3)", "over", "", (2, 1))[0],
                         SETTLE_PUSH)
        self.assertEqual(settle_pick("OU(3)", "under", "", (1, 2))[0],
                         SETTLE_PUSH)

    def test_composite_over_half_lost(self) -> None:
        """`2/2.5` 进 2 球买**大** → 输半（线 2 走水 + 线 2.5 输）。"""
        st, _ = settle_pick("OU", "over", "2/2.5", (1, 1))
        self.assertEqual(st, SETTLE_HALF_LOST)

    def test_composite_under_half_won(self) -> None:
        """`2/2.5` 进 2 球买**小** → 赢半（线 2 走水 + 线 2.5 赢）。"""
        st, _ = settle_pick("OU", "under", "2/2.5", (1, 1))
        self.assertEqual(st, SETTLE_HALF_WON)

    def test_composite_over_full_win(self) -> None:
        """`2/2.5` 进 3 球买大 → 两个半注都赢。"""
        st, _ = settle_pick("OU", "over", "2/2.5", (2, 1))
        self.assertEqual(st, SETTLE_WON)

    def test_line_from_code_when_blank(self) -> None:
        """line 为空时回退用盘口代码里的线值。"""
        self.assertEqual(settle_pick("OU(2.5)", "over", "", (2, 1))[0],
                         SETTLE_WON)

    def test_half_time_ou_uses_half_score(self) -> None:
        """上半场大小球用半场比分：半场 0:0 → 上半场小0.5 赢。"""
        st, _ = settle_pick("OU_1H(0.5)", "under", "", (3, 2), (0, 0))
        self.assertEqual(st, SETTLE_WON)


class TestAHSettlement(unittest.TestCase):
    """让球盘：line 以**主队**为基准（负 = 主队让球）。"""

    def test_home_gives_half_and_wins(self) -> None:
        """主队让 0.5 且赢 1 球 → 赢。"""
        self.assertEqual(settle_pick("AH(-0.5)", "home", "", (1, 0))[0],
                         SETTLE_WON)

    def test_home_gives_half_and_draws(self) -> None:
        """主队让 0.5 打平 → 输（让半球没有走水）。"""
        self.assertEqual(settle_pick("AH(-0.5)", "home", "", (1, 1))[0],
                         SETTLE_LOST)

    def test_away_receives_half_and_draws(self) -> None:
        """客队受让 0.5 打平 → 赢。"""
        self.assertEqual(settle_pick("AH(-0.5)", "away", "", (1, 1))[0],
                         SETTLE_WON)

    def test_integer_line_push(self) -> None:
        """让 1 球正好赢 1 球 → 走水。"""
        self.assertEqual(settle_pick("AH(-1)", "home", "", (2, 1))[0],
                         SETTLE_PUSH)
        self.assertEqual(settle_pick("AH(-1)", "away", "", (2, 1))[0],
                         SETTLE_PUSH)

    def test_composite_home_gives_half_lost(self) -> None:
        """主队**让** `0/0.5` 且打平 → 输半（线 0 走水 + 线 -0.5 输）。

        注意符号：`line` 以主队为基准，**负 = 主队让球**，
        所以「主队让 0.25」写作 `0/-0.5`，不是 `0/0.5`。
        """
        st, _ = settle_pick("AH", "home", "0/-0.5", (1, 1))
        self.assertEqual(st, SETTLE_HALF_LOST)

    def test_composite_away_receives_half_won(self) -> None:
        """同一盘口买客队受让 → 赢半。"""
        st, _ = settle_pick("AH", "away", "0/-0.5", (1, 1))
        self.assertEqual(st, SETTLE_HALF_WON)

    def test_composite_home_receives_half_won(self) -> None:
        """主队**受让** `0/0.5` 且打平 → 赢半（线 0 走水 + 线 +0.5 赢）。"""
        st, _ = settle_pick("AH", "home", "0/0.5", (1, 1))
        self.assertEqual(st, SETTLE_HALF_WON)

    def test_underdog_line_positive(self) -> None:
        """line 为正 = 主队受让：主队输 1 球但受让 1.5 → 赢。"""
        self.assertEqual(settle_pick("AH(1.5)", "home", "", (0, 1))[0],
                         SETTLE_WON)

    def test_half_time_ah_uses_half_score(self) -> None:
        st, _ = settle_pick("AH_1H(-0.5)", "home", "", (0, 3), (1, 0))
        self.assertEqual(st, SETTLE_WON)


class TestVoidAndHonesty(unittest.TestCase):
    """无法结算时必须 void —— 绝不能猜（诚实性红线）。"""

    def test_missing_half_score_is_void(self) -> None:
        """上半场盘口缺半场比分 → void，**不是**输。"""
        st, note = settle_pick("OU_1H(1.5)", "over", "", (2, 1), None)
        self.assertEqual(st, SETTLE_VOID)
        self.assertIn("半场比分", note)

    def test_missing_full_score_is_void(self) -> None:
        st, note = settle_pick("OU(2.5)", "over", "", None)
        self.assertEqual(st, SETTLE_VOID)
        self.assertIn("全场比分", note)

    def test_unknown_market_is_void(self) -> None:
        st, _ = settle_pick("XYZ(1)", "over", "", (1, 1))
        self.assertEqual(st, SETTLE_VOID)

    def test_bad_outcome_is_void(self) -> None:
        st, _ = settle_pick("OU(2.5)", "sideways", "", (1, 1))
        self.assertEqual(st, SETTLE_VOID)

    def test_had_with_over_outcome_is_void(self) -> None:
        """结果名与盘口族不匹配 → void（不得当成输）。"""
        st, _ = settle_pick("HAD", "over", "", (1, 1))
        self.assertEqual(st, SETTLE_VOID)

    def test_score_as_dict(self) -> None:
        st, _ = settle_pick("OU(2.5)", "over", "", {"home": 2, "away": 1})
        self.assertEqual(st, SETTLE_WON)

    def test_malformed_score_is_void(self) -> None:
        st, _ = settle_pick("OU(2.5)", "over", "", "not-a-score")
        self.assertEqual(st, SETTLE_VOID)


class TestPnl(unittest.TestCase):
    """pnl 是**每单位本金**的净收益。"""

    def test_win(self) -> None:
        self.assertAlmostEqual(pnl_for(SETTLE_WON, 2.0), 1.0)
        self.assertAlmostEqual(pnl_for(SETTLE_WON, 3.5), 2.5)

    def test_loss_is_minus_one(self) -> None:
        self.assertAlmostEqual(pnl_for(SETTLE_LOST, 2.0), -1.0)

    def test_push_returns_stake(self) -> None:
        self.assertAlmostEqual(pnl_for(SETTLE_PUSH, 2.0), 0.0)

    def test_half_won(self) -> None:
        """赢半 = 半个注赢、半个注退回。"""
        self.assertAlmostEqual(pnl_for(SETTLE_HALF_WON, 2.0), 0.5)

    def test_half_lost(self) -> None:
        self.assertAlmostEqual(pnl_for(SETTLE_HALF_LOST, 2.0), -0.5)


class TestCLV(unittest.TestCase):
    """CLV = 收盘价 / 买入价 - 1（正 = 买得比收盘便宜）。"""

    def test_positive_clv(self) -> None:
        got = clv(2.0, 2.2)
        self.assertIsNotNone(got)
        assert got is not None          # 收窄类型，供静态检查
        self.assertAlmostEqual(got, 0.1, places=6)

    def test_negative_clv(self) -> None:
        got = clv(2.2, 2.0)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertAlmostEqual(got, -0.090909, places=5)

    def test_invalid_odds_returns_none(self) -> None:
        self.assertIsNone(clv(0.0, 2.0))
        self.assertIsNone(clv(2.0, 0.0))
        self.assertIsNone(clv(1.0, 2.0))
        self.assertIsNone(clv(None, 2.0))  # type: ignore[arg-type]


def _row(status: str, pnl: float, odds: float = 2.0, is_pick: bool = True,
         closing: float = 0.0) -> Dict[str, Any]:
    return {"status": status, "pnl": pnl, "odds": odds, "is_pick": is_pick,
            "closing_odds": closing}


class TestSummarise(unittest.TestCase):
    """本地统计：命中率 / ROI / CLV。"""

    def test_hit_rate_excludes_push(self) -> None:
        rows = [_row(SETTLE_WON, 1.0), _row(SETTLE_LOST, -1.0),
                _row(SETTLE_PUSH, 0.0)]
        s = summarise(rows)
        self.assertEqual(s["won"], 1)
        self.assertEqual(s["lost"], 1)
        self.assertEqual(s["push"], 1)
        # 走水既非赢也非输 → 不进分母
        self.assertAlmostEqual(s["hit_rate"], 0.5)

    def test_roi(self) -> None:
        rows = [_row(SETTLE_WON, 1.0), _row(SETTLE_LOST, -1.0)]
        s = summarise(rows)
        self.assertAlmostEqual(s["roi"], 0.0)
        self.assertEqual(s["stake_units"], 2.0)

    def test_half_results_count_as_win_loss(self) -> None:
        """赢半算赢、输半算输（命中率语义）。"""
        rows = [_row(SETTLE_HALF_WON, 0.5), _row(SETTLE_HALF_LOST, -0.5)]
        s = summarise(rows)
        self.assertEqual(s["won"], 1)
        self.assertEqual(s["lost"], 1)

    def test_void_excluded_from_roi(self) -> None:
        """void 不得进入收益统计（否则系统性低估）。"""
        rows = [_row(SETTLE_WON, 1.0), _row(SETTLE_VOID, 0.0)]
        s = summarise(rows)
        self.assertEqual(s["void"], 1)
        self.assertEqual(s["stake_units"], 1.0)
        self.assertAlmostEqual(s["roi"], 1.0)

    def test_pending_excluded_from_roi(self) -> None:
        rows = [_row(SETTLE_WON, 1.0), _row(SETTLE_PENDING, 0.0)]
        s = summarise(rows)
        self.assertEqual(s["pending"], 1)
        self.assertEqual(s["stake_units"], 1.0)

    def test_unpicked_rows_do_not_dilute_roi(self) -> None:
        """**关键回归**：被门控拦截的盘口没投钱，不能算进本金。

        台账同时记录被拦截的盘口（用于校准阈值）。若把它们也算成
        1 单位本金，ROI 会被稀释到接近 0，得出“模型不赚钱”的错误结论。
        """
        rows = [
            _row(SETTLE_WON, 1.0, is_pick=True),
            _row(SETTLE_LOST, 0.0, is_pick=False),
            _row(SETTLE_LOST, 0.0, is_pick=False),
            _row(SETTLE_LOST, 0.0, is_pick=False),
        ]
        s = summarise(rows)
        self.assertEqual(s["stake_units"], 1.0)   # 只算真正下注的那 1 注
        self.assertAlmostEqual(s["roi"], 1.0)     # 而不是 1/4
        # 被拦截的单独统计，用于判断门控是否在帮忙
        self.assertEqual(s["unpicked_n"], 3)
        self.assertAlmostEqual(s["unpicked_hit_rate"], 0.0)

    def test_clv_stats(self) -> None:
        rows = [_row(SETTLE_WON, 1.0, odds=2.0, closing=2.2),
                _row(SETTLE_LOST, -1.0, odds=2.2, closing=2.0)]
        s = summarise(rows)
        self.assertEqual(s["clv_n"], 2)
        self.assertAlmostEqual(s["clv_positive_rate"], 0.5)

    def test_empty_rows(self) -> None:
        s = summarise([])
        self.assertEqual(s["total"], 0)
        self.assertIsNone(s["hit_rate"])
        self.assertIsNone(s["roi"])

    def test_dirty_numeric_fields_do_not_crash(self) -> None:
        """脏数据（字符串/None）不能让整次统计失败。"""
        rows = [{"status": SETTLE_WON, "pnl": "oops", "odds": None,
                 "is_pick": True, "closing_odds": "bad"}]
        s = summarise(rows)
        self.assertEqual(s["won"], 1)
        self.assertAlmostEqual(s["roi"], 0.0)


class TestLedgerPersistence(unittest.TestCase):
    """台账落盘/重读/去重。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _entry(self, **kw: Any) -> LedgerEntry:
        base: Dict[str, Any] = dict(
            at="2024-01-01T00:00:00+00:00", match_id="m1", market="OU(2.5)",
            line="", outcome="over", odds=2.0, is_pick=True)
        base.update(kw)
        return LedgerEntry(**base)

    def test_append_and_load_roundtrip(self) -> None:
        led = DecisionLedger(self.root)
        n = led._append([self._entry(), self._entry(outcome="under")])
        self.assertEqual(n, 2)
        rows = led.load()
        self.assertEqual(len(rows), 2)

    def test_duplicate_key_keeps_latest(self) -> None:
        """同一盘口被多次决策 → 只保留**最新**一条（否则重复计数）。"""
        led = DecisionLedger(self.root)
        led._append([self._entry(at="2024-01-01T00:00:00+00:00", odds=2.0)])
        led._append([self._entry(at="2024-01-02T00:00:00+00:00", odds=1.8)])
        rows = led.load()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0].odds, 1.8)

    def test_disabled_without_root(self) -> None:
        led = DecisionLedger(None)
        self.assertFalse(led.enabled)
        self.assertEqual(led.record_match(mock.MagicMock()), 0)
        self.assertEqual(led.load(), [])

    def test_settle_updates_status_and_pnl(self) -> None:
        led = DecisionLedger(self.root)
        led._append([self._entry(market="OU(2.5)", outcome="over", odds=2.0)])
        out = led.settle({"m1": {"ft": [2, 1]}})
        self.assertEqual(out["settled"], 1)
        rows = led.load()
        self.assertEqual(rows[0].status, SETTLE_WON)
        self.assertAlmostEqual(rows[0].pnl, 1.0)

    def test_settle_skips_unknown_match(self) -> None:
        led = DecisionLedger(self.root)
        led._append([self._entry()])
        out = led.settle({"other": {"ft": [1, 0]}})
        self.assertEqual(out["skipped"], 1)
        self.assertEqual(led.load()[0].status, SETTLE_PENDING)

    def test_settle_void_for_half_market_without_ht(self) -> None:
        """缺半场比分 → void（不得用全场比分硬算上半场盘口）。"""
        led = DecisionLedger(self.root)
        led._append([self._entry(market="OU_1H(1.5)", outcome="over")])
        out = led.settle({"m1": {"ft": [2, 1]}})
        self.assertEqual(out["void"], 1)
        self.assertEqual(led.load()[0].status, SETTLE_VOID)

    def test_settle_is_idempotent(self) -> None:
        """已结算的条目不会被重复结算（否则 pnl 会累加）。"""
        led = DecisionLedger(self.root)
        led._append([self._entry(odds=2.0)])
        led.settle({"m1": {"ft": [2, 1]}})
        led.settle({"m1": {"ft": [2, 1]}})
        self.assertAlmostEqual(led.load()[0].pnl, 1.0)

    def test_settle_accepts_bare_tuple(self) -> None:
        led = DecisionLedger(self.root)
        led._append([self._entry(market="HAD", outcome="home")])
        led.settle({"m1": (1, 0)})
        self.assertEqual(led.load()[0].status, SETTLE_WON)

    def test_closing_odds_recorded_for_clv(self) -> None:
        led = DecisionLedger(self.root)
        e = self._entry(market="HAD", outcome="home", odds=2.0)
        led._append([e])
        key = "|".join(str(x) for x in e.key)
        led.settle({"m1": {"ft": [1, 0], "closing": {key: 2.2}}})
        row = led.load()[0]
        self.assertAlmostEqual(row.closing_odds, 2.2)
        self.assertAlmostEqual(row.clv or 0.0, 0.1, places=6)

    def test_stats_only_picks_by_default(self) -> None:
        led = DecisionLedger(self.root)
        led._append([
            self._entry(outcome="over", is_pick=True),
            self._entry(outcome="under", is_pick=False),
        ])
        led.settle({"m1": {"ft": [2, 1]}})
        picks = led.stats()
        allrows = led.stats(only_picks=False)
        self.assertEqual(picks["total"], 1)
        self.assertEqual(allrows["total"], 2)

    def test_stats_grouped_by_trigger(self) -> None:
        led = DecisionLedger(self.root)
        led._append([self._entry(trigger="price_change")])
        led.settle({"m1": {"ft": [2, 1]}})
        self.assertIn("price_change", led.stats()["by_trigger"])

    def test_corrupt_line_is_skipped(self) -> None:
        """坏行不能让整次读取失败。"""
        led = DecisionLedger(self.root)
        led._append([self._entry()])
        path = led.path
        assert path is not None
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("{{{ not json\n")
        self.assertEqual(len(led.load()), 1)

    def test_health(self) -> None:
        led = DecisionLedger(self.root)
        h = led.health()
        self.assertTrue(h["enabled"])
        self.assertIn("ledger.jsonl", h["path"])


class TestLedgerFromMatchResult(unittest.TestCase):
    """`record_match` 从 `MatchPicks` 提取留痕（含被拦截的盘口）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.led = DecisionLedger(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _comp(self, market: str, line: str = "") -> Any:
        from service.match_decision import MarketComputation
        c = MarketComputation(
            market=market, outcomes=("over", "under"),
            odds=(2.0, 1.9), p_fair=(0.5, 0.5), edges=(0.0, 0.0),
            kellys=(0.0, 0.0), line=line)
        return c

    def test_records_picks_and_rejected(self) -> None:
        from core.entry_gate import GateResult
        from service.match_decision import MatchPicks

        comp = self._comp("OU(2.5)")
        # 一条通过、一条被拦截（带原因码）。
        # `margin` 是由 `edge - required_edge` 派生的属性，不能直接赋值。
        comp.gates = (
            GateResult(passed=True, outcome="over", edge=0.05,
                       required_edge=0.02, rejects=()),
            GateResult(passed=False, outcome="under", edge=0.01,
                       required_edge=0.02,
                       rejects=("edge_below_threshold",)),
        )
        mp = MatchPicks(match_id="m1", league="L", home="A", away="B")
        mp.computations = [comp]
        mp.picks = [{
            "market": "OU(2.5)", "line": "", "outcome": "over",
            # 乐鱼风格中文标签（用户要求：说「全场进球数>2.5」，
            # 而不是「全场大2.5」或 `OU(2.5) over`）。
            "pick_label": "全场进球数>2.5", "odds": 2.0, "edge": 0.05,
            "required_edge": 0.02, "confidence": 0.4, "p_market": 0.5,
            "p_llm": 0.6, "p_fused": 0.55, "llm_weight": 0.4, "kelly": 0.02,
        }]
        n = self.led.record_match(mp, trigger="price_change")
        self.assertEqual(n, 2)          # pick 一条 + 被拦截一条
        rows = self.led.load()
        picks = [r for r in rows if r.is_pick]
        rej = [r for r in rows if not r.is_pick]
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0].label, "全场进球数>2.5")
        self.assertEqual(picks[0].trigger, "price_change")
        self.assertAlmostEqual(picks[0].p_llm, 0.6)
        self.assertEqual(len(rej), 1)
        self.assertIn("edge_below_threshold", rej[0].rejects)

    def test_no_match_id_is_noop(self) -> None:
        from service.match_decision import MatchPicks
        self.assertEqual(self.led.record_match(MatchPicks(match_id="")), 0)

    def test_entry_rejects_computed_property(self) -> None:
        """`reject_reasons` 是从 gates 派生的属性，必须能取到。"""
        comp = self._comp("OU(2.5)")
        self.assertEqual(comp.reject_reasons, [])


class TestHalfScoreParsing(unittest.TestCase):
    """`LEYUMatch.half_score`：S0 = 半场，S1 = 全场。"""

    def test_half_and_full(self) -> None:
        from collector.leyu_client import LEYUMatch
        m = LEYUMatch(mid="1", sport_id="1", sport="足球", tid="t",
                      tournament="T", home="A", away="B", start_ms=0,
                      status=3, score_raw="S0|0:1,S1|2:1")
        self.assertEqual(m.score, (2, 1))
        self.assertEqual(m.half_score, (0, 1))

    def test_missing_half_returns_none(self) -> None:
        from collector.leyu_client import LEYUMatch
        m = LEYUMatch(mid="1", sport_id="1", sport="足球", tid="t",
                      tournament="T", home="A", away="B", start_ms=0,
                      status=3, score_raw="S1|2:1")
        self.assertEqual(m.half_score, (None, None))

    def test_s10_not_matched_as_s1(self) -> None:
        """`S10|` 不能被 `S1` 前缀命中（已有回归用例的同类风险）。"""
        from collector.leyu_client import LEYUMatch
        m = LEYUMatch(mid="1", sport_id="1", sport="足球", tid="t",
                      tournament="T", home="A", away="B", start_ms=0,
                      status=3, score_raw="S10|9:9,S1|1:2,S0|0:1")
        self.assertEqual(m.score, (1, 2))
        self.assertEqual(m.half_score, (0, 1))


class TestAnalysisServiceLedgerWiring(unittest.TestCase):
    """台账必须真的被决策链路调用（否则统计永远是空的）。"""

    def _svc(self, **kw: Any) -> Any:
        from service.analysis import AnalysisConfig, AnalysisService
        return AnalysisService(valuation=mock.MagicMock(), realtime=None,
                               config=AnalysisConfig(use_llm=False, **kw))

    def test_ledger_created_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            svc = self._svc(ledger_root=d)
            self.assertTrue(svc.ledger.enabled)

    def test_settler_requires_ledger(self) -> None:
        svc = self._svc(ledger_root=None)
        self.assertFalse(svc.start_settler())

    def test_settler_disabled_when_interval_zero(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            svc = self._svc(ledger_root=d, settle_interval_s=0.0)
            self.assertFalse(svc.start_settler())

    def test_settle_finished_no_pending_is_cheap(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            svc = self._svc(ledger_root=d)
            out = svc.settle_finished()
            self.assertEqual(out["settled"], 0)

    def test_ledger_stats_shape(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            svc = self._svc(ledger_root=d)
            st = svc.ledger_stats()
            self.assertIn("roi", st)
            self.assertIn("ledger", st)
            self.assertIn("settle", st)

    def test_cycle_writes_ledger(self) -> None:
        """定时决策必须落台账（用户要的“记录决策结果”）。"""
        import inspect

        from service.analysis import AnalysisService
        self.assertIn("_record_ledger",
                      inspect.getsource(AnalysisService.decide_list))

    def test_trigger_writes_ledger(self) -> None:
        import inspect

        from service.analysis import AnalysisService
        self.assertIn("_record_ledger",
                      inspect.getsource(AnalysisService._merge_into_latest))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestClosingOddsCapture(unittest.TestCase):
    """回归：收盘赔率必须被留痕，否则 CLV 永远算不出来。

    真实缺口：`settle_finished()` 从不填 `scores[...]["closing"]`，
    于是 `closing_odds` 恒为 0 → `summarise()` 里 `clv_n=0`、
    `clv_mean=None`。而 CLV 是判断「是否持续拿到好价格」的
    唯一领先指标（`core/entry_gate.py` 证据 [E]），
    用户问“LLM 准不准”时最有用的指标直接缺失。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _entry(self, **kw: Any) -> LedgerEntry:
        base: Dict[str, Any] = dict(
            at="2024-01-01T00:00:00+00:00", match_id="m1", market="OU(2.5)",
            line="", outcome="over", odds=2.00, is_pick=True)
        base.update(kw)
        return LedgerEntry(**base)

    def test_capture_closing_sets_odds(self) -> None:
        led = DecisionLedger(self.root)
        led._append([self._entry()])
        n = led.capture_closing({("m1", "OU(2.5)", "", "over"): 2.20})
        self.assertEqual(n, 1)
        rows = led.load()
        self.assertAlmostEqual(rows[0].closing_odds, 2.20)

    def test_clv_computed_after_capture(self) -> None:
        led = DecisionLedger(self.root)
        led._append([self._entry(odds=2.00)])
        led.capture_closing({("m1", "OU(2.5)", "", "over"): 2.20})
        row = led.load()[0]
        clv = row.clv
        assert clv is not None
        self.assertAlmostEqual(clv, 0.10, places=6)

    def test_stats_expose_clv_mean(self) -> None:
        led = DecisionLedger(self.root)
        led._append([self._entry(odds=2.00)])
        led.capture_closing({("m1", "OU(2.5)", "", "over"): 2.20})
        # CLV 只对**已结算**（graded）的注统计，因此先结算
        led.settle({"m1": {"ft": (2, 1)}})
        st = led.stats(only_picks=False)
        self.assertEqual(st["clv_n"], 1)
        self.assertIsNotNone(st["clv_mean"])

    def test_invalid_odds_not_written(self) -> None:
        led = DecisionLedger(self.root)
        led._append([self._entry()])
        bads: Any = (0.0, 1.0, -2.0, "abc", None)
        for bad in bads:
            self.assertEqual(
                led.capture_closing({("m1", "OU(2.5)", "", "over"): bad}), 0)

    def test_settled_entries_are_not_overwritten(self) -> None:
        """已结算条目不得被改回 pending（否则统计会回退）。"""
        led = DecisionLedger(self.root)
        led._append([self._entry()])
        led.capture_closing({("m1", "OU(2.5)", "", "over"): 2.20})
        led.settle({"m1": {"ft": (2, 1)}})
        self.assertNotEqual(led.load()[0].status, "pending")
        n = led.capture_closing({("m1", "OU(2.5)", "", "over"): 9.99})
        self.assertEqual(n, 0)
        self.assertAlmostEqual(led.load()[0].closing_odds, 2.20)

    def test_pending_match_ids(self) -> None:
        led = DecisionLedger(self.root)
        led._append([self._entry(match_id="m1"),
                     self._entry(match_id="m2", outcome="under")])
        self.assertEqual(sorted(led.pending_match_ids()), ["m1", "m2"])

    def test_service_captures_closing_from_snapshots(self) -> None:
        """端到端：settle_finished 前会从快照库补收盘赔率。"""
        from service.analysis import AnalysisConfig, AnalysisService

        with tempfile.TemporaryDirectory() as d:
            svc = AnalysisService(valuation=mock.MagicMock(), realtime=None,
                                  config=AnalysisConfig(use_llm=False,
                                                        ledger_root=d))
            svc.ledger._append([
                LedgerEntry(at="2024-01-01T00:00:00+00:00", match_id="m1",
                            market="HAD", line="", outcome="home",
                            odds=2.50, is_pick=True)])
            snap = mock.MagicMock()
            snap.market = "HAD"
            snap.outcomes = ("home", "draw", "away")
            snap.odds = (2.10, 3.30, 3.40)
            snap.metadata = {"leyu_hv": ""}
            svc.valuation._snapshots_of.return_value = [snap]
            # 只更新**台账里已存在**的 pending 条目：本例只录了 HAD/home，
            # 因此 draw/away 不会被凭空新建（避免台账膨胀）。
            n = svc._capture_closing_odds({"m1"})
            self.assertEqual(n, 1)
            row = [r for r in svc.ledger.load()
                   if r.key == ("m1", "HAD", "", "home")][0]
            self.assertAlmostEqual(row.closing_odds, 2.10)
            self.assertEqual(
                [r for r in svc.ledger.load() if r.key[3] == "draw"], [])


class TestLedgerRootInjectedAfterConstruction(unittest.TestCase):
    """回归：API 层**后注入** ledger_root 时，台账必须真的启用。

    真实故障：`api/app.py` 在构造完 `AnalysisService` 之后才用
    `dataclasses.replace` 注入 `ledger_root`（它需要先拿到快照根目录
    才能算出输出路径）。而 `self.ledger` 是在 `__init__` 里按**当时的**
    配置创建的，于是注入永远不生效：

        [entrypoint] 提示：赛后结算未启用（台账不可用或间隔为 0）

    后果：`/ledger/stats` 恒为零条记录，用户问“LLM 准不准”时
    没有任何本地数据可查 —— 正是用户报告的第三个问题。
    """

    def test_injecting_ledger_root_enables_ledger(self) -> None:
        from dataclasses import replace

        from service.analysis import AnalysisConfig, AnalysisService

        svc = AnalysisService(valuation=mock.MagicMock(), realtime=None,
                              config=AnalysisConfig(use_llm=False))
        self.assertFalse(svc.ledger.enabled)      # 初始未注入 → 空实现

        with tempfile.TemporaryDirectory() as d:
            svc.config = replace(svc.config, ledger_root=d)
            self.assertTrue(svc.ledger.enabled)
            self.assertEqual(str(svc.ledger.root), d)
            self.assertTrue(svc.start_settler())
            self.assertTrue(svc.settler_running)
            svc.stop_settler()

    def test_same_root_keeps_existing_ledger_object(self) -> None:
        from dataclasses import replace

        from service.analysis import AnalysisConfig, AnalysisService

        with tempfile.TemporaryDirectory() as d:
            svc = AnalysisService(valuation=mock.MagicMock(), realtime=None,
                                  config=AnalysisConfig(use_llm=False,
                                                        ledger_root=d))
            before = svc.ledger
            svc.config = replace(svc.config, cache_ttl_s=99.0)
            self.assertIs(svc.ledger, before)     # 未改路径则不重建

    def test_module_level_config_field_still_mutates(self) -> None:
        """兼容既有用法：`ana.config = replace(ana.config, X=...)`。"""
        from dataclasses import replace

        from service.analysis import AnalysisConfig, AnalysisService

        svc = AnalysisService(valuation=mock.MagicMock(), realtime=None,
                              config=AnalysisConfig(use_llm=False))
        svc.config = replace(svc.config, settle_interval_s=7.0)
        self.assertEqual(svc.config.settle_interval_s, 7.0)


class TestResultPathInjectedAfterConstruction(unittest.TestCase):
    """**严重回归**：API 层后注入 `result_path` 时，必须读回上次决策结果。

    用户报告「3000 端口加载不出来比赛场次」，实测根因就在此处：

        # api/app.py 的真实顺序（必须先有快照根目录才能算输出路径）
        ana = build_analysis_service(svc)        # ← 此刻 result_path=None
        ana.config = replace(ana.config,
                             result_path=... )   # ← 之后才注入

    而 `_restore_latest()` 只在 `__init__` 里跑过一次，那时路径还是空的，
    直接 return → `_latest` 永远是 None → 接口返回 `pending=True`、
    `count=0` → 页面只能显示「等待后台首轮决策」。

    后果特别严重是因为**一轮全量决策要 50+ 分钟**：结果一产出就被下一轮
    覆盖，用户几乎永远看不到列表。这与 `ledger_root` 属同一类初始化时序
    缺陷（见 `TestLedgerRootInjectedAfterConstruction`）。
    """

    def _svc(self, **kw: Any) -> Any:
        from service.analysis import AnalysisConfig, AnalysisService
        return AnalysisService(valuation=mock.MagicMock(), realtime=None,
                               config=AnalysisConfig(use_llm=False, **kw))

    def _write_result(self, path: str, minutes_ago: float = 5.0) -> Dict[str, Any]:
        from datetime import timedelta
        payload = {
            "count": 2,
            "summary": {"n": 2, "buy": 1},
            "decisions": [{"match_id": "m1"}, {"match_id": "m2"}],
            "finished_at": (datetime.now(timezone.utc)
                            - timedelta(minutes=minutes_ago)).isoformat(),
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        return payload

    def test_injecting_result_path_restores_latest(self) -> None:
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "decisions.json")
            self._write_result(p)
            svc = self._svc()                       # 初始 result_path=None
            self.assertIsNone(svc.latest_result())
            svc.config = replace(svc.config, result_path=p)
            res = svc.latest_result()
            self.assertIsNotNone(res, "注入 result_path 后必须读回上次结果")
            self.assertEqual(res["count"], 2)

    def test_latest_age_s_reports_age(self) -> None:
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "decisions.json")
            self._write_result(p, minutes_ago=53.0)
            svc = self._svc()
            svc.config = replace(svc.config, result_path=p)
            age = svc.latest_age_s()
            assert age is not None
            self.assertGreater(age, 50 * 60)

    def test_latest_age_s_none_without_result(self) -> None:
        self.assertIsNone(self._svc().latest_age_s())

    def test_same_path_keeps_latest_object(self) -> None:
        """路径未变时不重建/不重读（避免每次改配置都读盘）。"""
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "decisions.json")
            self._write_result(p)
            svc = self._svc(result_path=p)
            before = svc.latest_result()
            svc.config = replace(svc.config, cache_ttl_s=99.0)
            self.assertIs(svc.latest_result(), before)


class TestStaleResultsAreShownNotHidden(unittest.TestCase):
    """**回归**：旧结果必须返回并标注 stale，而不是被隐藏。

    早期实现是「超龄就返回 None」，于是页面永远空白 ——
    而磁盘上明明有几千场决策。把「慢」伪装成「无」会让用户
    完全看不出问题在哪，也无从自行排查。
    """

    def test_old_result_is_returned_with_stale_flag(self) -> None:
        from api.app import create_app
        with tempfile.TemporaryDirectory() as d, \
                tempfile.TemporaryDirectory() as out:
            p = os.path.join(out, "decisions.json")
            import json
            from datetime import timedelta
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({
                    "count": 2436, "summary": {"n": 2436},
                    "decisions": [{"match_id": "m1"}],
                    "finished_at": (datetime.now(timezone.utc)
                                    - timedelta(minutes=53)).isoformat(),
                }, fh)
            app = create_app(snapshot_root=d, corpus_root="output",
                             source="ticai")
            app.svc.ingest_corpus()
            from dataclasses import replace
            app.analysis.config = replace(app.analysis.config,
                                          result_path=p)
            st, body = app.dispatch(
                "GET", "/api/v1/decisions", {"max_age": ["1800"]}, {})
            self.assertEqual(st, 200)
            self.assertEqual(body["count"], 2436, "旧数据也应返回")
            self.assertTrue(body["stale"], "必须如实标注 stale")
            self.assertIsNotNone(body["age_s"])
            self.assertFalse(body.get("pending"))

    def test_fresh_result_is_not_stale(self) -> None:
        from api.app import create_app
        with tempfile.TemporaryDirectory() as d, \
                tempfile.TemporaryDirectory() as out:
            p = os.path.join(out, "decisions.json")
            import json
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({
                    "count": 1, "summary": {"n": 1},
                    "decisions": [{"match_id": "m1"}],
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }, fh)
            app = create_app(snapshot_root=d, corpus_root="output",
                             source="ticai")
            app.svc.ingest_corpus()
            from dataclasses import replace
            app.analysis.config = replace(app.analysis.config,
                                          result_path=p)
            _, body = app.dispatch(
                "GET", "/api/v1/decisions", {"max_age": ["1800"]}, {})
            self.assertEqual(body["count"], 1)
            self.assertFalse(body["stale"])

    def test_no_result_at_all_is_pending(self) -> None:
        from api.app import create_app
        with tempfile.TemporaryDirectory() as d:
            app = create_app(snapshot_root=d, corpus_root="output",
                             source="ticai")
            app.svc.ingest_corpus()
            _, body = app.dispatch("GET", "/api/v1/decisions",
                                   {"max_age": ["1800"]}, {})
            self.assertTrue(body.get("pending"))
            self.assertEqual(body["count"], 0)


class TestSettleScoresFromLocalPush(unittest.TestCase):
    """**回归**：会话过期（拿不到 REST 赛程）时，正确率仍要能统计出来。

    用户报告「本地买入决策的正确率没有做统计」。根因：结算只读
    `source.schedule()`，而会话一过期（`6001 token已过期`）它就抛异常，
    于是 `graded=0` → **命中率/ROI/CLV 永远算不出来**。
    而推送里的比分（`C103`）与结束通知（`C109`）本就在内存里。

    ⚠️ 安全红线：只结算**已确认结束**（`C109`）的场次。
    进球过程中推送的是**当前比分**，拿它结算会把“还在踢”的比赛
    算成已定输赢 —— 那种统计比没有统计更危险。
    """

    def _svc(self, **kw: Any) -> Any:
        from service.analysis import AnalysisConfig, AnalysisService
        return AnalysisService(valuation=mock.MagicMock(), realtime=None,
                               config=AnalysisConfig(use_llm=False, **kw))

    def test_only_finished_matches_are_settled(self) -> None:
        svc = self._svc()
        hub = mock.MagicMock()
        hub.scores_snapshot.return_value = {"m1": (2, 1), "m2": (0, 0)}
        hub.finished_mids.return_value = ["m1"]      # 只有 m1 确认结束
        hub.half_score.return_value = None
        svc.realtime = hub
        scores: Dict[str, Any] = {}
        n = svc._merge_local_scores(scores, {"m1", "m2"})
        self.assertEqual(n, 1)
        self.assertEqual(list(scores), ["m1"])
        self.assertEqual(scores["m1"]["ft"], [2, 1])

    def test_rest_scores_take_precedence(self) -> None:
        svc = self._svc()
        hub = mock.MagicMock()
        hub.scores_snapshot.return_value = {"m1": (9, 9)}
        hub.finished_mids.return_value = ["m1"]
        svc.realtime = hub
        scores = {"m1": {"ft": [1, 0]}}
        svc._merge_local_scores(scores, {"m1"})
        self.assertEqual(scores["m1"]["ft"], [1, 0], "REST 结果不得被覆盖")

    def test_pending_matches_not_touched(self) -> None:
        svc = self._svc()
        hub = mock.MagicMock()
        hub.scores_snapshot.return_value = {"other": (1, 1)}
        hub.finished_mids.return_value = ["other"]
        svc.realtime = hub
        scores: Dict[str, Any] = {}
        self.assertEqual(svc._merge_local_scores(scores, {"m1"}), 0)
        self.assertEqual(scores, {})

    def test_no_realtime_is_safe(self) -> None:
        svc = self._svc()
        self.assertEqual(svc._merge_local_scores({}, {"m1"}), 0)

    def test_old_hub_without_capability_is_safe(self) -> None:
        """旧版 Hub 没有这些方法时不得抛异常（只用 getattr 宽容检测）。"""
        svc = self._svc()
        svc.realtime = object()          # 完全没有 scores_snapshot
        self.assertEqual(svc._merge_local_scores({}, {"m1"}), 0)

    def test_dirty_score_is_skipped(self) -> None:
        svc = self._svc()
        hub = mock.MagicMock()
        hub.scores_snapshot.return_value = {"m1": ("a", "b")}
        hub.finished_mids.return_value = ["m1"]
        svc.realtime = hub
        scores: Dict[str, Any] = {}
        self.assertEqual(svc._merge_local_scores(scores, {"m1"}), 0)
        self.assertEqual(scores, {})

    def test_half_score_attached_when_available(self) -> None:
        svc = self._svc()
        hub = mock.MagicMock()
        hub.scores_snapshot.return_value = {"m1": (2, 1)}
        hub.finished_mids.return_value = ["m1"]
        hub.half_score.return_value = (1, 0)
        svc.realtime = hub
        scores: Dict[str, Any] = {}
        svc._merge_local_scores(scores, {"m1"})
        self.assertEqual(scores["m1"]["ht"], [1, 0])

    def test_settle_reports_local_source(self) -> None:
        """`settle_finished` 要把「有多少场来自推送」回传，便于排查。"""
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            svc = self._svc(ledger_root=d)
            hub = mock.MagicMock()
            hub.scores_snapshot.return_value = {}
            hub.finished_mids.return_value = []
            svc.realtime = hub
            out = svc.settle_finished()
            self.assertIn("scores_from_push", out)
            self.assertIn("scores_total", out)


class TestAccuracyStatsEndToEnd(unittest.TestCase):
    """**端到端**：会话过期（拿不到 REST 赛程）时，正确率照样能算出来。

    这是用户问题 2「本地买入决策的正确率没有做统计」的验收口径 ——
    用**推送比分**完成结算并产出命中率/ROI/CLV，全程不依赖外部服务。
    """

    def test_accuracy_is_computed_from_local_scores(self) -> None:
        from service.analysis import AnalysisConfig, AnalysisService

        with tempfile.TemporaryDirectory() as d:
            svc = AnalysisService(valuation=mock.MagicMock(), realtime=None,
                                  config=AnalysisConfig(use_llm=False,
                                                        ledger_root=d))
            # 三条买入建议：m1 会赢、m2/m3 会输
            svc.ledger._append([
                LedgerEntry(at="2024-01-01T00:00:00+00:00", match_id="m1",
                            market="HAD", line="", outcome="home", odds=2.0,
                            is_pick=True),
                LedgerEntry(at="2024-01-01T00:00:00+00:00", match_id="m2",
                            market="HAD", line="", outcome="away", odds=3.0,
                            is_pick=True),
                LedgerEntry(at="2024-01-01T00:00:00+00:00", match_id="m3",
                            market="HAD", line="", outcome="home", odds=1.8,
                            is_pick=True),
            ])
            hub = mock.MagicMock()
            hub.scores_snapshot.return_value = {"m1": (2, 0), "m2": (1, 0),
                                               "m3": (0, 1)}
            hub.finished_mids.return_value = ["m1", "m2", "m3"]
            hub.half_score.return_value = None
            svc.realtime = hub

            out = svc.settle_finished()
            self.assertEqual(out["scores_from_push"], 3,
                             "3 场都应来自推送比分兜底")
            st = out["stats"]
            self.assertEqual(st["graded"], 3)
            self.assertEqual(st["won"], 1)
            self.assertEqual(st["lost"], 2)
            assert st["hit_rate"] is not None
            self.assertAlmostEqual(st["hit_rate"], 1 / 3, places=4)
            assert st["roi"] is not None
            self.assertLess(st["roi"], 0, "1 赢 2 输 + 高赔未命中 → 负收益")

    def test_api_exposes_accuracy_fields(self) -> None:
        """`/ledger/stats` 必须带命中率/ROI/CLV 字段（前端面板依赖）。"""
        from api.app import create_app
        with tempfile.TemporaryDirectory() as d:
            app = create_app(snapshot_root=d, corpus_root="output",
                             source="ticai")
            app.svc.ingest_corpus()
            _, body = app.dispatch("GET", "/api/v1/ledger/stats", {}, {})
            for k in ("hit_rate", "roi", "clv_n", "clv_mean", "graded",
                      "pending", "unpicked_hit_rate", "ledger"):
                self.assertIn(k, body, "缺少字段 %s（前端面板需要）" % k)


class TestFinishedMatchesFetchScoresViaOdds(unittest.TestCase):
    """**根因回归**：赛程接口不带比分，必须用 `odds()` 补，否则永远 graded=0。

    实测（本项目真实缺陷，用户问题 2「买入决策正确率没有统计」的根因）：
      * `source.schedule()`（`getOriginalDataPB`）**不返回 `msc` 字段** ——
        全部已结束赛事的 `score_raw` 都是空串，`score` 恒为 `(None, None)`；
      * `source.odds(mids)`（`structureMatchBaseInfoByMidsPB`）**带 `msc`**
        （`S0|0:1,S1|1:2,…`），是全仓唯一可靠的赛果来源。

    因此 `_finished_matches()` 必须用 `odds()` 补比分，否则
    `settle_finished()` 永远集不到赛果 → `graded=0` → 命中率算不出来。
    """

    class _M:
        def __init__(self, mid, finished=True, score=(None, None)):
            self.mid = mid
            self._finished = finished
            self.score = score
            self.half_score = (None, None)

        @property
        def is_finished(self) -> bool:
            return self._finished

    def _svc(self, schedule, odds):
        from service.analysis import AnalysisConfig, AnalysisService
        val = mock.MagicMock()
        val.source.schedule.return_value = schedule
        val.source.odds.side_effect = odds
        return AnalysisService(valuation=val, realtime=None,
                               config=AnalysisConfig(use_llm=False))

    def test_scores_fetched_via_odds_when_schedule_lacks_them(self) -> None:
        sched = [self._M("m1"), self._M("m2", finished=False)]
        detailed = [self._M("m1", score=(2, 1))]
        svc = self._svc(sched, lambda mids: detailed)
        out = svc._finished_matches(mids={"m1"})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].score, (2, 1), "必须用 odds() 补回比分")
        # 只对关心的 mid 调上游
        svc.valuation.source.odds.assert_called_once()
        self.assertEqual(list(svc.valuation.source.odds.call_args[0][0]), ["m1"])

    def test_only_pending_mids_requested(self) -> None:
        """只为待结算场次补比分（避免为几千场无关赛事打上游）。"""
        sched = [self._M("m1"), self._M("m2"), self._M("m3")]
        svc = self._svc(sched, lambda mids: [])
        svc._finished_matches(mids={"m2"})
        self.assertEqual(list(svc.valuation.source.odds.call_args[0][0]), ["m2"])

    def test_no_finished_returns_empty_without_calling_odds(self) -> None:
        svc = self._svc([self._M("m1", finished=False)], lambda mids: [])
        self.assertEqual(svc._finished_matches(mids={"m1"}), [])
        svc.valuation.source.odds.assert_not_called()

    def test_schedule_with_scores_skips_odds_call(self) -> None:
        """若赛程已带比分（理论上不会），则不额外打上游。"""
        sched = [self._M("m1", score=(1, 0))]
        svc = self._svc(sched, lambda mids: [])
        out = svc._finished_matches(mids={"m1"})
        self.assertEqual(out[0].score, (1, 0))
        svc.valuation.source.odds.assert_not_called()

    def test_odds_failure_degrades_to_empty(self) -> None:
        """补比分失败时返回空（不崩），由后续轮次重试。"""
        def boom(_mids):
            raise RuntimeError("上游不可用")
        svc = self._svc([self._M("m1")], boom)
        self.assertEqual(svc._finished_matches(mids={"m1"}), [])

    def test_schedule_failure_degrades_to_empty(self) -> None:
        from service.analysis import AnalysisConfig, AnalysisService
        val = mock.MagicMock()
        val.source.schedule.side_effect = RuntimeError("token 过期")
        svc = AnalysisService(valuation=val, realtime=None,
                              config=AnalysisConfig(use_llm=False))
        self.assertEqual(svc._finished_matches(mids={"m1"}), [])


class TestConfirmedFinishedFromSchedule(unittest.TestCase):
    """**根因回归**：结束证据不能只认 `C109`，否则永远不结算。

    实测：`scores.json` 里有 **17 场**有比分却 `done=False`
    （`C109` 只覆盖推送存活期间且订阅到的场次），因此永远不被结算。

    修法：结束证据取**并集** ——
      1. `C109` 推送（实时，但覆盖窄）；
      2. **赛程 `ms==110`**（覆盖全，重启后仍有）。

    ⚠️ 安全前提不变：仍必须**确证结束**才结算，
    否则会把还在踢的比赛按当前比分算成已定输赢。
    """

    class _M:
        def __init__(self, mid, finished):
            self.mid = mid
            self._f = finished

        @property
        def is_finished(self) -> bool:
            return self._f

    def _svc(self, schedule):
        from service.analysis import AnalysisConfig, AnalysisService
        val = mock.MagicMock()
        val.source.schedule.return_value = schedule
        return AnalysisService(valuation=val, realtime=None,
                               config=AnalysisConfig(use_llm=False))

    def test_confirmed_only_includes_pending_and_finished(self) -> None:
        svc = self._svc([self._M("m1", True), self._M("m2", False),
                         self._M("m3", True)])
        got = svc._confirmed_finished_mids({"m1", "m2"})
        self.assertEqual(got, {"m1"},
                         "只取「待结算 ∩ 已结束」；m3 不在 pending 中")

    def test_confirmed_empty_when_schedule_fails(self) -> None:
        from service.analysis import AnalysisConfig, AnalysisService
        val = mock.MagicMock()
        val.source.schedule.side_effect = RuntimeError("token 过期")
        svc = AnalysisService(valuation=val, realtime=None,
                              config=AnalysisConfig(use_llm=False))
        self.assertEqual(svc._confirmed_finished_mids({"m1"}), set())

    def test_confirmed_empty_for_empty_pending(self) -> None:
        self.assertEqual(self._svc([self._M("m1", True)])
                         ._confirmed_finished_mids(set()), set())

    def test_merge_accepts_schedule_confirmed(self) -> None:
        """有了赛程确认，即使没收到 `C109` 也能结算。"""
        from service.analysis import AnalysisConfig, AnalysisService
        svc = AnalysisService(valuation=mock.MagicMock(), realtime=None,
                              config=AnalysisConfig(use_llm=False))
        hub = mock.MagicMock()
        hub.scores_snapshot.return_value = {"m1": (2, 1)}
        hub.finished_mids.return_value = []      # 没收到 C109
        hub.half_score.return_value = None
        svc.realtime = hub
        scores: Dict[str, Any] = {}
        n = svc._merge_local_scores(scores, {"m1"}, confirmed={"m1"})
        self.assertEqual(n, 1)
        self.assertEqual(scores["m1"]["ft"], [2, 1])

    def test_merge_still_rejects_unconfirmed(self) -> None:
        """**安全红线**：两路都没确认结束 → 绝不结算。"""
        from service.analysis import AnalysisConfig, AnalysisService
        svc = AnalysisService(valuation=mock.MagicMock(), realtime=None,
                              config=AnalysisConfig(use_llm=False))
        hub = mock.MagicMock()
        hub.scores_snapshot.return_value = {"m1": (1, 0)}
        hub.finished_mids.return_value = []
        svc.realtime = hub
        scores: Dict[str, Any] = {}
        self.assertEqual(svc._merge_local_scores(scores, {"m1"},
                                                 confirmed=set()), 0)
        self.assertEqual(scores, {})


class TestAccuracyEndToEndWithRealHub(unittest.TestCase):
    """**端到端（真实 Hub）**：推送比分 → 落盘 → 重启 → 结算 → 有命中率。

    这是用户问题 2 的完整验收：用**真实的 `RealtimeHub`**（而非 mock）
    走一遍「收推送 → 落盘 → 新实例回填 → 结算」，
    证明「正确率算不出来」的链路已被打通。

    背景：实测两条上游路径都拿不到**历史**比分
    （赛程不返回 `msc`；盘口接口对已结束场次返回空串），
    赛果只在「进行中」那个时间窗可得，而结算总在结束之后。
    """

    def test_push_then_restart_then_settle(self) -> None:
        import base64
        import gzip
        import json as _json
        import os as _os
        import tempfile

        from collector.leyu_realtime import RealtimeHub
        from service.analysis import AnalysisConfig, AnalysisService
        from service.ledger import LedgerEntry

        def _enc(obj: Any) -> str:
            return base64.b64encode(
                gzip.compress(_json.dumps(obj).encode())).decode()

        with tempfile.TemporaryDirectory() as d:
            trend_root = _os.path.join(d, "_trends")
            ledger_root = _os.path.join(d, "ledger")

            # 1) 真实 Hub 收到比分与结束通知
            hub = RealtimeHub(session_provider=None, trend_root=trend_root)
            hub._handle_message({"cmd": "C103", "cd": _enc(
                {"mid": "m1", "msc": ["S0|1:0", "S1|2:1"], "mst": "90"})})
            hub._handle_message({"cmd": "C109", "cd": _enc(
                [{"mid": "m1", "ms": 110}])})
            self.assertEqual(hub.score("m1"), (2, 1))
            self.assertTrue(hub.is_finished("m1"))

            # 2) **重启**：新实例必须从磁盘回填赛果
            hub2 = RealtimeHub(session_provider=None, trend_root=trend_root)
            self.assertEqual(hub2.score("m1"), (2, 1),
                             "重启后必须能回填比分")
            self.assertTrue(hub2.is_finished("m1"),
                            "重启后必须能回填结束状态")

            # 3) 台账里有一条该场的买入建议
            svc = AnalysisService(valuation=mock.MagicMock(), realtime=hub2,
                                  config=AnalysisConfig(use_llm=False,
                                                        ledger_root=ledger_root))
            svc.ledger._append([
                LedgerEntry(at="2024-01-01T00:00:00+00:00", match_id="m1",
                            market="HAD", line="", outcome="home", odds=2.0,
                            is_pick=True)])

            # 4) 结算：走 `_merge_local_scores`（C109 已确认结束）
            scores: Dict[str, Any] = {}
            n = svc._merge_local_scores(scores, {"m1"})
            self.assertEqual(n, 1)
            out = svc.ledger.settle(scores)
            self.assertEqual(out["settled"], 1)
            st = svc.ledger.stats()
            self.assertEqual(st["graded"], 1,
                             "必须真的产出已结算样本（否则正确率永远为空）")
            self.assertEqual(st["won"], 1)
            assert st["hit_rate"] is not None
            self.assertAlmostEqual(st["hit_rate"], 1.0, places=4)
