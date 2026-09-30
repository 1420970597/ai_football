#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
估值服务层 —— 编排 core + store，供 API 层调用

分层约束（AGENTS.md §3.2）：
  - 本层做业务编排，**只通过 store 访问数据**
  - API 层（api/app.py）只做 HTTP 编解码，不写业务逻辑
  - 本层不直接 import selenium（浏览器能力经 collector 走 HTTP）

报告依据：
  §8.1  去水五法交叉 + 分歧告警
  §8.2  优势估计
  §8.4  贝叶斯收缩
  §8.5  分数/多元 Kelly
  §8.8  校准闭环
  §13.2 L5（自动投注）刻意不实现
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from collector.normalizer import normalize_matches
from core import calibration as cal
from core import devig as devig_mod
from core import economics as econ
from core import microstructure as micro
from core.models import (
    DevigMethod,
    FairProbabilities,
    OddsSnapshot,
    SnapshotState,
)
from store import SnapshotStore, make_cache

__all__ = ["ValuationService", "load_corpus"]

DEFAULT_SOURCE = "体彩官方API"


def _as_prob_tuple(raw: object, name: str) -> Tuple[float, ...]:
    """把外部传入的概率序列安全转为 float 元组。

    覆盖 Decimal / numpy 标量 / 字符串数字等非原生类型；
    转换失败或越界一律抛 ValueError，并带上元素下标便于定位。
    """
    if not isinstance(raw, (list, tuple)):
        raise ValueError("%s 应为序列，实际为 %s" % (name, type(raw).__name__))
    out: List[float] = []
    for i, v in enumerate(raw):
        # 先做类型判定再用 str() 归一，避开 float(object) 的类型错误，
        # 同时仍支持 int / float / 字符串数字等常见来源。
        if isinstance(v, bool):
            raise ValueError("%s[%d] 不能是布尔值: %r" % (name, i, v))
        if isinstance(v, (int, float)):
            # 已确认为原生数值；仍包一层以防 numpy 标量 / 数值子类
            # 在超大值上转换异常，统一收口为 ValueError。
            try:
                x = float(v)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(
                    "%s[%d] 无法转为数值: %r" % (name, i, v)) from exc
        elif isinstance(v, (str, bytes)):
            try:
                x = float(str(v).strip())
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(
                    "%s[%d] 无法转为数值: %r" % (name, i, v)) from exc
        else:
            raise ValueError(
                "%s[%d] 类型不支持: %s" % (name, i, type(v).__name__))
        if not math.isfinite(x) or x < 0.0 or x > 1.0:
            raise ValueError("%s[%d] 必须是 [0,1] 内的有限概率: %r"
                             % (name, i, v))
        out.append(x)
    return tuple(out)


def load_corpus(root: str | Path) -> List[Dict[str, Any]]:
    """从 output/场次*.json 读取原始记录（官方 API V2 的落盘结果）。

    这是**合法数据源**：中国体育彩票官方 API，非爬取。
    """
    root = Path(root)
    out: List[Dict[str, Any]] = []
    for p in sorted(root.glob("场次*.json")):
        try:
            with p.open(encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                out.append(data)
            elif isinstance(data, list):
                out.extend(x for x in data if isinstance(x, dict))
        except (OSError, ValueError):
            continue
    return out


class ValuationService:
    """估值服务：快照 → 去水 → 优势 → 仓位 → 校准。"""

    def __init__(
        self,
        snapshot_root: str | Path,
        corpus_root: Optional[str | Path] = None,
        cache: Optional[Any] = None,
    ) -> None:
        self.store = SnapshotStore(
            snapshot_root,
            cache=cache if cache is not None else make_cache(prefer_redis=False),
        )
        self.corpus_root = Path(corpus_root) if corpus_root else None
        self._ingested = False

    # -- 数据准备 -----------------------------------------------------------

    def ingest_corpus(self, source: str = DEFAULT_SOURCE) -> Dict[str, Any]:
        """把 output/ 下的原始记录归一化并写入不可变快照存储。"""
        if self.corpus_root is None:
            return {"ingested": 0, "reason": "未配置 corpus_root"}
        records = load_corpus(self.corpus_root)
        res = normalize_matches(records, source=source)
        written = self.store.append_many(res.snapshots)
        self._ingested = True
        return {
            "records": len(records),
            "snapshots": len(res.snapshots),
            "written": len(written),
            "issues": len(res.issues),
            "summary": res.summary(),
        }

    def _all_snapshots(self) -> List[OddsSnapshot]:
        """扫描存储，返回全部快照。"""
        out: List[OddsSnapshot] = []
        for d in sorted(self.store.root.rglob("_index.json")):
            out.extend(self.store._load_dir(d.parent))
        return out

    # -- 查询 ---------------------------------------------------------------

    def list_matches(
        self,
        date: Optional[str] = None,
        league: Optional[str] = None,
        query: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """赛事列表（按 match_id 聚合）。

        关键细节：同一赛事有多个市场（1X2 / AH），各自有独立的采集时间。
        因此**每个市场各自取最新的快照**，而不是取全市场时间最晚的那一条 ——
        否则 latest_odds 会只剩一个市场，前端赔率列会为空。
        基本信息（队名/联赛/时间）优先从 1X2 快照取（保证一致性）。
        """
        snaps = self._all_snapshots()
        by_id: Dict[str, List[OddsSnapshot]] = {}
        for s in snaps:
            by_id.setdefault(s.match_id, []).append(s)

        out: List[Dict[str, Any]] = []
        for mid, group in by_id.items():
            group.sort(key=lambda s: s.captured_at)

            # 每个市场各自的最新快照
            latest_by_market: Dict[str, OddsSnapshot] = {}
            for s in group:
                latest_by_market[s.market] = s     # 时间有序，后者覆盖

            # 主记录：优先 1X2，否则取任一市场
            primary = latest_by_market.get("1X2") or group[-1]
            meta = dict(primary.metadata or {})
            d = str(meta.get("比赛日期", ""))
            if date and d != date:
                continue
            if league and primary.league != league:
                continue
            if query:
                hay = "%s %s %s %s" % (mid, primary.home, primary.away,
                                      primary.league)
                if query.lower() not in hay.lower():
                    continue

            # 状态取各市场中最差（最保守）：只要任一不可用就不标为有效
            states = [s.state for s in latest_by_market.values()]
            worst = min(
                states,
                key=lambda st: (st is SnapshotState.ACTIVE,
                                st is SnapshotState.STALE),
            ) if states else SnapshotState.ACTIVE

            out.append({
                "match_id": mid,
                "league": primary.league,
                "home": primary.home,
                "away": primary.away,
                "date": d,
                "time": str(meta.get("比赛时间", "")),
                "state": worst.value,
                "snapshot_count": len(group),
                "markets": sorted(latest_by_market),
                "latest_odds": {
                    mk: list(sn.odds)
                    for mk, sn in sorted(latest_by_market.items())
                },
                "latest_outcomes": {
                    mk: list(sn.outcomes)
                    for mk, sn in sorted(latest_by_market.items())
                },
            })
        out.sort(key=lambda x: (x.get("date") or "", x["match_id"]))
        return out

    def _snapshots_of(self, match_id: str) -> List[OddsSnapshot]:
        snaps = [s for s in self._all_snapshots() if s.match_id == match_id]
        snaps.sort(key=lambda s: s.captured_at)
        return snaps

    def _latest(self, match_id: str,
                market: str = "1X2") -> Optional[OddsSnapshot]:
        for s in reversed(self._snapshots_of(match_id)):
            if s.market == market:
                return s
        return None

    def get_match(self, match_id: str) -> Optional[Dict[str, Any]]:
        snaps = self._snapshots_of(match_id)
        if not snaps:
            return None
        latest = snaps[-1]
        return {
            "match_id": match_id,
            "league": latest.league,
            "home": latest.home,
            "away": latest.away,
            "metadata": dict(latest.metadata or {}),
            "snapshot_count": len(snaps),
            "snapshots": [s.as_dict() for s in snaps],
        }

    def get_odds(self, match_id: str) -> Optional[Dict[str, Any]]:
        snaps = self._snapshots_of(match_id)
        if not snaps:
            return None
        by_market: Dict[str, List[Dict[str, Any]]] = {}
        for s in snaps:
            by_market.setdefault(s.market, []).append(s.as_dict())
        return {"match_id": match_id, "markets": by_market}

    # -- 估值 ---------------------------------------------------------------

    def get_fair(self, match_id: str, market: str = "1X2",
                 method: str = "auto") -> Optional[Dict[str, Any]]:
        snap = self._latest(match_id, market)
        if snap is None:
            return None
        try:
            m = DevigMethod(method)
        except ValueError:
            m = DevigMethod.AUTO
        fp = devig_mod.devig(snap, method=m)
        d = fp.as_dict()
        d["match_id"] = match_id
        d["state"] = snap.state.value
        # 报告 §8.1：EV = −m/(1+m)，公平定价下的期望
        d["ev_if_fair"] = (
            econ.expected_value_from_margin(snap.margin)
            if snap.margin > -1 else None
        )
        return d

    def get_edge(
        self,
        match_id: str,
        market: str = "1X2",
        method: str = "auto",
        p_model: Optional[Sequence[float]] = None,
        n_obs: int = 0,
        sigma_p: Optional[float] = None,
        q_fill: float = 1.0,
        cost_exec: float = 0.0,
        stake: float = 100.0,
    ) -> Optional[Dict[str, Any]]:
        """优势估计。

        ⚠️ 关键诚实性设计（报告 §7.1/§8.2）：
        若未提供独立的 p_model，则**默认采用去水后的公平概率**，
        即"市场是对的"这一零信息假设。此时 edge 必然等于 −m/(1+m) < 0。
        这不是实现缺陷，而是报告核心结论的直接体现：
        **没有独立信息源，就不存在优势。**
        若上游提供了 p_model（如自建模型输出），则按报告 §8.4 做收缩后估计。
        """
        snap = self._latest(match_id, market)
        if snap is None:
            return None

        fair = self.get_fair(match_id, market, method)
        if fair is None:
            return None
        p_fair = _as_prob_tuple(fair["probabilities"], "p_fair")

        notes: List[str] = []
        if p_model is None:
            p_raw = p_fair
            notes.append(
                "未提供独立 p_model，按零信息假设使用公平概率作为模型概率；"
                "此时 edge 等于公平定价下的期望 −m/(1+m)，恒为负（报告 §8.2）"
            )
        else:
            p_raw = _as_prob_tuple(p_model, "p_model")
            if len(p_raw) != len(p_fair):
                raise ValueError(
                    "p_model 长度 %d 与市场结果数 %d 不一致"
                    % (len(p_raw), len(p_fair))
                )

        p_shrunk, w = econ.shrink_probabilities(
            p_raw, p_fair, n=n_obs, sigma_p=sigma_p,
            p_for_k=(sum(p_raw) / len(p_raw)) if p_raw else None,
        )
        edges = econ.edge_from_probabilities(p_shrunk, snap.odds)
        evs = edges
        ev_eff = tuple(
            econ.effective_ev(e, q_fill * econ.q_fill_model(e, stake), cost_exec)
            for e in evs
        )

        return {
            "match_id": match_id,
            "market": market,
            "state": snap.state.value,
            "odds": list(snap.odds),
            "outcomes": list(snap.outcomes),
            "p_fair": list(p_fair),
            "p_model_raw": list(p_raw),
            "p_model": list(p_shrunk),
            "edge": list(edges),
            "ev": list(evs),
            "ev_effective": list(ev_eff),
            "shrink_weight": round(w, 6),
            "margin": round(snap.margin, 6),
            "method_spread_pp": fair.get("method_spread_pp"),
            "spread_warning": fair.get("spread_warning"),
            "notes": notes + list(fair.get("notes") or []),
        }

    def get_microstructure(self, match_id: str,
                           market: str = "1X2") -> Optional[Dict[str, Any]]:
        snaps = [s for s in self._snapshots_of(match_id) if s.market == market]
        if not snaps:
            return None
        signals = []
        n_out = len(snaps[0].outcomes)
        for i in range(n_out):
            try:
                sig = micro.extract_signals(snaps, outcome_index=i)
            except ValueError:
                continue
            signals.append(sig.as_dict())
        return {
            "match_id": match_id,
            "market": market,
            "n_snapshots": len(snaps),
            "signals": signals,
        }

    def get_portfolio(
        self,
        match_id: str,
        market: str = "1X2",
        lam: float = 0.25,
        rho: float = 0.0,
        max_total_exposure: float = 0.25,
    ) -> Optional[Dict[str, Any]]:
        """仓位建议。

        ⚠️ 研究用途，**不构成投注建议**；L5 执行层刻意不实现（报告 §5.3/§13.2）。
        """
        e = self.get_edge(match_id, market)
        if e is None:
            return None
        res = econ.sizing(
            outcomes=e["outcomes"],
            probs=e["p_model"],
            odds=e["odds"],
            edges=e["edge"],
            lam=lam,
            rho=rho,
            max_total_exposure=max_total_exposure,
        )
        d = res.as_dict()
        d["match_id"] = match_id
        d["market"] = market
        d["disclaimer"] = "研究用途，不构成投注建议；本系统不实现投注执行"
        d["execution"] = {
            "auto_betting": "NOT_SUPPORTED",
            "reason": "REPORT §5.3 成交裁定权在对手方；§9.3 触发条件为注单质量",
        }
        return d

    def get_calibration(
        self,
        equity: Optional[Sequence[float]] = None,
        n_bins: int = 10,
    ) -> Dict[str, Any]:
        """校准报告。

        本服务默认**没有已结算样本**（不采集结算结果），因此返回空报告并说明；
        若调用方提供 probs/outcomes，可由上层计算。
        """
        rep = cal.build_calibration_report(
            probs=[], outcomes=[], equity=equity, n_bins=n_bins)
        d = rep.as_dict()
        d["note"] = (
            "本原型不采集结算结果，故默认无校准样本。"
            "接入结算数据后 Brier/ECE/CLV 将自动生效（报告 §8.8）"
        )
        return d

    # -- 采集 ---------------------------------------------------------------

    def start_collect(self, urls: Sequence[str],
                      registry: Any = None) -> Dict[str, Any]:
        """触发采集任务（经 HTTP 委托 browser-scraper，不直接操作浏览器）。"""
        from collector.orchestrator import ScrapeClient, collect_urls

        client = ScrapeClient()
        task, results = collect_urls(
            urls, client=client, registry=registry, require_healthy=False)
        ok = sum(1 for r in results if r.get("success"))
        return {"task": task.as_dict(), "results_ok": ok,
                "results_total": len(results)}

    def health(self) -> Dict[str, Any]:
        return {
            "status": "healthy",
            "snapshot_store": self.store.stats(),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
