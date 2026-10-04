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
import tempfile
import unittest
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
        self.assertAlmostEqual(clv(2.0, 2.2), 0.1, places=6)

    def test_negative_clv(self) -> None:
        self.assertAlmostEqual(clv(2.2, 2.0), -0.090909, places=5)

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
            "pick_label": "全场大2.5", "odds": 2.0, "edge": 0.05,
            "required_edge": 0.02, "confidence": 0.4, "p_market": 0.5,
            "p_llm": 0.6, "p_fused": 0.55, "llm_weight": 0.4, "kelly": 0.02,
        }]
        n = self.led.record_match(mp, trigger="price_change")
        self.assertEqual(n, 2)          # pick 一条 + 被拦截一条
        rows = self.led.load()
        picks = [r for r in rows if r.is_pick]
        rej = [r for r in rows if not r.is_pick]
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0].label, "全场大2.5")
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
