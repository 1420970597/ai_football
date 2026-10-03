#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据源切换（collector.sources + service.valuation 接线）的单元测试。

覆盖：
  - 源名归一化（leyu / ticai / 中文别名 / 未知源报错）
  - 乐鱼归一化器：chpid → market 映射、盘口线解析、状态推导、结果缺失降级
  - **让球盘必须两向（AH）而非三向（HHAD）** —— 这是一个已修复的真实缺陷回归
  - 会话失效（code=0401013）必须抛 AuthError 并给出可操作提示
  - saz 离线回放可与在线模式产出等价的快照，且不发起任何网络请求

运行：``python3 -m unittest tests.test_sources -v``
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping

from collector.leyu_client import (
    AuthError,
    DecodeError,
    MarketQuote,
    OddsQuote,
    LEYUMatch,
    decode_envelope,
    parse_odds_block,
)
from collector.leyu_normalizer import (
    DEFAULT_STALE_AFTER,
    normalize_leyu_matches,
    parse_hv,
    snapshots_from_market,
    spec_for_market_quote,
)
from collector.sources import (
    KNOWN_SOURCES,
    SOURCE_LEYU,
    SOURCE_TICAI,
    LEYUSource,
    SourceError,
    SnapshotSource,
    TicaiFileSource,
    make_source,
    resolve_source_name,
)
from core.models import SnapshotState
from core.markets import MarketSpec

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAZ = os.path.join(_ROOT, "leyu.saz")

#: 抓包中赔率变更时间的毫秒戳（1790785713151 → 2026-09-30T16:28:33Z）
CAPTURE_MS = 1790785713151
CAPTURED = datetime.fromtimestamp(CAPTURE_MS / 1000.0, tz=timezone.utc)


def _mq(chpid: str, name: str, hpt: int, hv: str = "",
        quotes=(), ctsp: int = CAPTURE_MS) -> MarketQuote:
    """构造一个 MarketQuote（测试用）。"""
    return MarketQuote(
        chpid=chpid, name=name, hpt=hpt, hv=hv,
        quotes=tuple(quotes), ctsp=ctsp,
    )


def _q(outcome: str, decimal: float, label: str = "") -> OddsQuote:
    return OddsQuote(oid="o", outcome=outcome, label=label or outcome,
                     decimal=decimal, line="")


def _match(markets=(), ms: int = 0, mid: str = "5714088") -> LEYUMatch:
    return LEYUMatch(
        mid=mid, sport_id="1", sport="足球", tid="217",
        tournament="非洲国家杯2027资格赛",
        home="厄立特里亚", away="南非",
        start_ms=1790784000000, status=ms, markets=tuple(markets),
    )


def _spec(mq: MarketQuote) -> MarketSpec:
    """取出映射结果并断言可映射（类型安全，避免 Optional 访问告警）。"""
    spec = spec_for_market_quote(mq)
    if spec is None:
        raise AssertionError("盘口 chpid=%s 未能映射为 MarketSpec" % mq.chpid)
    return spec


# --------------------------------------------------------------------------- #
# 源名归一化
# --------------------------------------------------------------------------- #

class TestSourceResolution(unittest.TestCase):
    def test_default_is_leyu(self) -> None:
        # 系统已切换：不传源名必须落到乐鱼
        self.assertEqual(resolve_source_name(None), SOURCE_LEYU)
        self.assertEqual(resolve_source_name(""), SOURCE_LEYU)
        self.assertEqual(resolve_source_name("   "), SOURCE_LEYU)

    def test_aliases(self) -> None:
        for name in ("leyu", "LeYu", "乐鱼", "乐鱼API", "乐鱼官方api"):
            with self.subTest(name=name):
                self.assertEqual(resolve_source_name(name), SOURCE_LEYU)
        for name in ("ticai", "体彩", "体彩官方API", "sporttery"):
            with self.subTest(name=name):
                self.assertEqual(resolve_source_name(name), SOURCE_TICAI)

    def test_unknown_source_raises(self) -> None:
        with self.assertRaises(SourceError):
            resolve_source_name("bogus-source")

    def test_known_sources_lists_both(self) -> None:
        self.assertEqual(set(KNOWN_SOURCES), {SOURCE_LEYU, SOURCE_TICAI})

    def test_make_source_rejects_missing_corpus_for_ticai(self) -> None:
        with self.assertRaises(SourceError):
            make_source("ticai", corpus_root=None)

    def test_sources_are_snapshot_sources(self) -> None:
        self.assertIsInstance(make_source("ticai", corpus_root=_ROOT),
                              SnapshotSource)
        self.assertIsInstance(make_source("leyu"), SnapshotSource)

    def test_display_source_differs_from_source_id(self) -> None:
        # 规范 ID 用于路由，展示名写入快照——两者不可混用
        src = make_source("leyu")
        self.assertEqual(src.name, "leyu")
        self.assertEqual(src.display_source, "乐鱼API")
        t = make_source("ticai", corpus_root=_ROOT)
        self.assertEqual(t.name, "ticai")
        self.assertEqual(t.display_source, "体彩官方API")


# --------------------------------------------------------------------------- #
# 盘口线解析
# --------------------------------------------------------------------------- #

class TestParseHv(unittest.TestCase):
    def test_plain_lines(self) -> None:
        self.assertEqual(parse_hv("1.5"), 1.5)
        self.assertEqual(parse_hv("-1"), -1.0)
        self.assertEqual(parse_hv("0"), 0.0)

    def test_composite_lines_take_midpoint(self) -> None:
        # 亚盘复合线取中点，便于以单一数值表达
        self.assertEqual(parse_hv("1/1.5"), 1.25)
        self.assertEqual(parse_hv("0/0.5"), 0.25)
        self.assertEqual(parse_hv("-0.5/1"), 0.25)

    def test_empty_line_for_winner_markets(self) -> None:
        self.assertIsNone(parse_hv(""))
        self.assertIsNone(parse_hv(None))
        self.assertIsNone(parse_hv("   "))

    def test_aggregate_buckets_are_not_lines(self) -> None:
        # "3+"/"7+" 是聚合桶，不是连续让球线
        self.assertIsNone(parse_hv("3+"))
        self.assertIsNone(parse_hv("7+"))

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(parse_hv("abc"))


# --------------------------------------------------------------------------- #
# market 映射（含真实缺陷回归）
# --------------------------------------------------------------------------- #

class TestMarketMapping(unittest.TestCase):
    def test_1x2_maps_to_had(self) -> None:
        spec = _spec(_mq("1", "全场独赢", 1))
        self.assertEqual(spec.code, "HAD")
        self.assertEqual(spec.outcomes, ("home", "draw", "away"))

    def test_handicap_is_two_way_AH_not_three_way_HHAD(self) -> None:
        """回归：乐鱼让球盘只有 2 个选项，绝不能映射为三向 HHAD。

        早期缺陷：映射成 HHAD 会让每个让球盘都因「缺少结果 draw」被丢弃。
        """
        spec = _spec(_mq("4", "全场让球", 2, hv="0.5"))
        self.assertEqual(spec.n_outcomes, 2)
        self.assertEqual(spec.outcomes, ("home", "away"))
        self.assertTrue(spec.code.startswith("AH("))
        # 线值必须原样透传（不做符号翻转）
        self.assertEqual(spec.line, 0.5)

    def test_handicap_negative_line_preserved(self) -> None:
        self.assertEqual(_spec(_mq("4", "全场让球", 2, hv="-0.5")).line, -0.5)

    def test_ou_needs_a_line(self) -> None:
        self.assertIsNone(spec_for_market_quote(_mq("2", "全场大小", 5, hv="")))
        spec = _spec(_mq("2", "全场大小", 5, hv="2.5"))
        self.assertEqual(spec.outcomes, ("over", "under"))
        self.assertEqual(spec.line, 2.5)

    def test_first_half_markets_are_distinct_codes(self) -> None:
        win = spec_for_market_quote(_mq("17", "上半场独赢", 1))
        ah = spec_for_market_quote(_mq("19", "上半场让球", 2, hv="0.5"))
        ou = spec_for_market_quote(_mq("18", "上半场大小", 5, hv="1.5"))
        assert win is not None and ah is not None and ou is not None
        self.assertEqual(win.code, "HAD_1H")
        self.assertEqual(ah.code, "AH_1H(0.5)")
        self.assertEqual(ou.code, "OU_1H(1.5)")
        # 半场玩法不得与全场混用 code（否则跨玩法校验会张冠李戴）
        self.assertNotEqual(ah.code, "AH(0.5)")

    def test_unknown_chpid_returns_none(self) -> None:
        self.assertIsNone(spec_for_market_quote(_mq("999", "神秘盘口", 9)))


# --------------------------------------------------------------------------- #
# 归一化
# --------------------------------------------------------------------------- #

class TestNormalization(unittest.TestCase):
    def test_1x2_snapshot_odds_and_metadata(self) -> None:
        m = _match([_mq("1", "全场独赢", 1, quotes=[
            _q("home", 32.0, "主胜"), _q("draw", 8.5, "平局"), _q("away", 1.07, "客胜"),
        ])])
        snaps, issues = normalize_leyu_matches([m], captured=CAPTURED)
        self.assertEqual(len(snaps), 1)
        self.assertEqual(issues, [])
        s = snaps[0]
        self.assertEqual(s.market, "HAD")
        self.assertEqual(s.odds, (32.0, 8.5, 1.07))
        self.assertEqual(s.source, "乐鱼API")
        self.assertEqual(s.league, "非洲国家杯2027资格赛")
        # 溯源字段必须保留
        self.assertEqual(s.metadata["leyu_chpid"], "1")
        self.assertIn("leyu_cds", s.metadata)

    def test_handicap_snapshot_is_two_outcome(self) -> None:
        m = _match([_mq("4", "全场让球", 2, hv="1", quotes=[
            _q("home", 2.11, "+1"), _q("away", 1.78, "-1"),
        ])])
        snaps, issues = normalize_leyu_matches([m], captured=CAPTURED)
        self.assertEqual(issues, [])
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0].outcomes, ("home", "away"))
        self.assertEqual(snaps[0].odds, (2.11, 1.78))
        self.assertEqual(snaps[0].metadata["盘口线"], 1.0)

    def test_missing_outcome_is_reported_not_silently_dropped(self) -> None:
        m = _match([_mq("1", "全场独赢", 1, quotes=[
            _q("home", 2.0), _q("away", 3.0),   # 缺 draw
        ])])
        snaps, issues = normalize_leyu_matches([m], captured=CAPTURED)
        self.assertEqual(snaps, [])
        self.assertEqual(len(issues), 1)
        self.assertIn("draw", issues[0])

    def test_unmappable_market_is_reported(self) -> None:
        m = _match([_mq("999", "神秘盘口", 9, quotes=[_q("home", 2.0)])])
        snaps, issues = normalize_leyu_matches([m], captured=CAPTURED)
        self.assertEqual(snaps, [])
        self.assertIn("无法映射", issues[0])

    def test_finished_match_is_delisted(self) -> None:
        m = _match([_mq("1", "全场独赢", 1, quotes=[
            _q("home", 2.0), _q("draw", 3.0), _q("away", 4.0),
        ])], ms=110)
        snaps, _ = normalize_leyu_matches([m], captured=CAPTURED)
        self.assertEqual(snaps[0].state, SnapshotState.DELISTED)

    def test_stale_when_quote_older_than_threshold(self) -> None:
        old = int((CAPTURED.timestamp() - 3600) * 1000)
        m = _match([_mq("1", "全场独赢", 1, ctsp=old, quotes=[
            _q("home", 2.0), _q("draw", 3.0), _q("away", 4.0),
        ])])
        snaps, _ = normalize_leyu_matches([m], captured=CAPTURED,
                                          stale_after=DEFAULT_STALE_AFTER)
        self.assertEqual(snaps[0].state, SnapshotState.STALE)

    def test_fresh_quote_is_active(self) -> None:
        fresh = int((CAPTURED.timestamp() - 5) * 1000)
        m = _match([_mq("1", "全场独赢", 1, ctsp=fresh, quotes=[
            _q("home", 2.0), _q("draw", 3.0), _q("away", 4.0),
        ])])
        snaps, _ = normalize_leyu_matches([m], captured=CAPTURED)
        self.assertEqual(snaps[0].state, SnapshotState.ACTIVE)

    def test_ctsp_is_taken_from_block_not_option(self) -> None:
        """回归：ctsp 在块级；早先实现从 ol[] 取导致新鲜度恒为 0。"""
        old = int((CAPTURED.timestamp() - 7200) * 1000)
        mq = _mq("1", "全场独赢", 1, ctsp=old, quotes=[
            _q("home", 2.0), _q("draw", 3.0), _q("away", 4.0),
        ])
        m = _match([mq])
        snaps, _ = normalize_leyu_matches([m], captured=CAPTURED)
        self.assertEqual(snaps[0].metadata["leyu_ctsp"], old)
        self.assertEqual(snaps[0].state, SnapshotState.STALE)

    def test_booksum_is_sanity_checkable(self) -> None:
        m = _match([_mq("1", "全场独赢", 1, quotes=[
            _q("home", 32.0), _q("draw", 8.5), _q("away", 1.07),
        ])])
        snaps, _ = normalize_leyu_matches([m], captured=CAPTURED)
        # 1/32 + 1/8.5 + 1/1.07 ≈ 1.0835 → 水位约 8.35%
        self.assertAlmostEqual(snaps[0].booksum, 1.0835, places=3)

    def test_uses_leyu_client_auth_error_type(self) -> None:
        self.assertTrue(issubclass(AuthError, DecodeError))


# --------------------------------------------------------------------------- #
# 会话鉴权（实测行为）
# --------------------------------------------------------------------------- #

class TestAuthReality(unittest.TestCase):
    """实测：随机 requestId 访问业务端点 → code=0401013 账户信息已过期。"""

    @staticmethod
    def _auth_payload(code: str = "0401013") -> Dict[str, Any]:
        return {"code": code, "data": "", "msg": "账户信息已过期,请重新登录"}

    def test_expired_session_raises_auth_error(self) -> None:
        with self.assertRaises(AuthError) as ctx:
            decode_envelope(self._auth_payload())
        # 错误信息必须给出可操作提示，而不是只说「失败」
        msg = str(ctx.exception)
        self.assertIn("0401013", msg)
        self.assertIn("requestId", msg)
        self.assertIn("会话", msg)

    def test_auth_error_is_also_decode_error(self) -> None:
        # 旧代码用 DecodeError 捕获，必须保持兼容
        with self.assertRaises(DecodeError):
            decode_envelope(self._auth_payload())

    def test_other_business_error_is_plain_decode_error(self) -> None:
        with self.assertRaises(DecodeError) as ctx:
            decode_envelope({"code": "9999999", "data": "", "msg": "系统繁忙"})
        self.assertNotIsInstance(ctx.exception, AuthError)

    def test_auth_error_codes_are_declared(self) -> None:
        from collector.leyu_client import AUTH_ERROR_CODES
        self.assertIn("0401013", AUTH_ERROR_CODES)


# --------------------------------------------------------------------------- #
# 离线回放 vs 在线（不联网）
# --------------------------------------------------------------------------- #

class TestReplaySource(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(SAZ):
            raise unittest.SkipTest("leyu.saz 不存在，跳过回放用例")

    def test_replay_fetch_produces_snapshots(self) -> None:
        src = LEYUSource(replay=SAZ)
        snaps, issues = src.fetch(captured=CAPTURED)
        self.assertEqual(issues, [])
        self.assertGreater(len(snaps), 100)
        self.assertEqual({s.source for s in snaps}, {"乐鱼API"})
        # 每个快照的 source 必须是展示名，而非规范 ID
        self.assertNotIn("leyu", {s.source for s in snaps})

    def test_replay_covers_all_six_market_families(self) -> None:
        src = LEYUSource(replay=SAZ)
        snaps, _ = src.fetch(captured=CAPTURED)
        codes = {s.market for s in snaps}
        self.assertTrue(any(c == "HAD" for c in codes))
        self.assertTrue(any(c.startswith("AH(") for c in codes))
        self.assertTrue(any(c.startswith("OU(") for c in codes))
        self.assertIn("HAD_1H", codes)
        self.assertTrue(any(c.startswith("AH_1H(") for c in codes))
        self.assertTrue(any(c.startswith("OU_1H(") for c in codes))

    def test_replay_handicap_matches_captured_odds(self) -> None:
        """让球盘必须是两向，且赔率与抓包一致（1.5 线：1.57 / 2.44）。"""
        src = LEYUSource(replay=SAZ)
        snaps, _ = src.fetch(captured=CAPTURED)
        target = [s for s in snaps
                  if s.match_id == "5714088" and s.market == "AH(1.5)"]
        self.assertEqual(len(target), 1)
        self.assertEqual(target[0].outcomes, ("home", "away"))
        self.assertEqual(tuple(round(o, 2) for o in target[0].odds), (1.57, 2.44))

    def test_replay_is_deterministic(self) -> None:
        src = LEYUSource(replay=SAZ)
        a, _ = src.fetch(captured=CAPTURED)
        b, _ = src.fetch(captured=CAPTURED)
        self.assertEqual([(s.market, s.odds) for s in a],
                         [(s.market, s.odds) for s in b])

    def test_missing_saz_raises_source_error(self) -> None:
        src = LEYUSource(replay="/nonexistent/leyu.saz")
        with self.assertRaises(SourceError):
            src.fetch(captured=CAPTURED)

    def test_describe_reports_mode(self) -> None:
        self.assertEqual(LEYUSource(replay=SAZ).describe()["mode"], "replay")
        self.assertEqual(LEYUSource().describe()["mode"], "online")

    def test_ticai_source_still_works(self) -> None:
        """数据源无关性的证明：换回体彩源仍能产出快照。"""
        src = TicaiFileSource(os.path.join(_ROOT, "output"))
        snaps, _ = src.fetch()
        self.assertGreater(len(snaps), 0)
        self.assertEqual({s.source for s in snaps}, {"体彩官方API"})


if __name__ == "__main__":
    unittest.main(verbosity=2)


# --------------------------------------------------------------------------- #
# 真实在线通路（默认跳过，用 AI_FOOTBALL_ONLINE=1 开启）
# --------------------------------------------------------------------------- #

def _online_creds():
    """从环境读在线会话；缺任一项返回 None。"""
    rid = (os.environ.get("LEYU_REQUEST_ID") or "").strip()
    host = (os.environ.get("LEYU_HOST") or "").strip()
    if not rid or not host:
        return None
    return host, rid


@unittest.skipUnless(os.environ.get("AI_FOOTBALL_ONLINE") == "1",
                     "在线用例默认跳过（设 AI_FOOTBALL_ONLINE=1 开启）")
class TestOnlineReality(unittest.TestCase):
    """对真实网关的行为锁定（需有效会话）。"""

    @classmethod
    def setUpClass(cls) -> None:
        creds = _online_creds()
        if creds is None:
            raise unittest.SkipTest(
                "需要 LEYU_HOST 与 LEYU_REQUEST_ID（有效会话）")
        cls.host, cls.rid = creds

    def _client(self):
        from collector.leyu_client import LEYUClient
        return LEYUClient(host=self.host, request_id=self.rid, timeout=20)

    def test_valid_session_returns_schedule(self) -> None:
        from collector.sources import SOCCER_SPORT_ID, LEYUSource
        src = LEYUSource(host=self.host, request_id=self.rid)
        matches = src.schedule()
        self.assertGreater(len(matches), 100)
        # 足球必须占绝对多数
        soccer = [m for m in matches if m.sport_id == SOCCER_SPORT_ID]
        self.assertGreater(len(soccer), 50)

    def test_default_selection_is_soccer_and_clean(self) -> None:
        """默认必须只选足球，且不产生「无法映射盘口」噪音。"""
        from collector.sources import LEYUSource
        src = LEYUSource(host=self.host, request_id=self.rid)
        snaps, issues = src.fetch(max_matches=8)
        self.assertGreater(len(snaps), 0)
        self.assertEqual(issues, [], "默认不应有盘口映射噪音")
        # 全部快照必须来自足球赛事
        mids = {s.match_id for s in snaps}
        soccer_ids = {m.mid for m in src.schedule()
                      if m.sport_id == "1"}
        self.assertTrue(mids <= soccer_ids)

    def test_random_session_is_rejected(self) -> None:
        """随机 requestId 必须抛 AuthError（业务层会话校验）。"""
        from collector.leyu_client import AuthError, LEYUClient
        c = LEYUClient(host=self.host, request_id="0" * 32, timeout=15)
        with self.assertRaises(AuthError):
            c.all_matches()

    def test_server_time_is_plaintext_scalar(self) -> None:
        """回归：getSystemTime 的 data 是明文数字字符串。"""
        ms = self._client().server_time_ms()
        self.assertGreater(ms, 1_600_000_000_000)
