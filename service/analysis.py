#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一分析服务（T7）—— 串联实时推送、快照存储、决策引擎与 LLM。

## 职责

    RealtimeHub（实时推送/走势）
         │
         ├─► 触发重新采集（盘口变动时刷新快照）      ← 保持与乐鱼同频
         │
    ValuationService（快照/去水/EV）
         │
         └─► DecisionEngine（小模型 ⊕ LLM）
                   │
                   └─► 决策结果 → API → 控制台列表

## 为什么需要这一层

`ValuationService` 只做「快照 → 去水/EV」，不感知实时推送，也不做决策。
`DecisionEngine` 只做单市场决策，不知道哪场比赛值得分析。
本层负责**编排**：选取候选赛事、附上走势与上下文、调用决策、排序缓存。

## 缓存策略

决策结果缓存 `cache_ttl_s`（默认 120 秒）。因为：
1. LLM 调用有成本与延迟（实测单场 3~10 秒）
2. 控制台每次刷新都要列表，不能每次都重算
3. 但也不能太久——盘口在动，决策必须跟随

缓存键含 `analysis_version`：参数变化时自动失效，避免脏结果。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from collector.leyu_realtime import RealtimeHub
from core.models import OddsSnapshot

from .decision import (
    DECISION_AVOID,
    DECISION_BUY,
    DECISION_NO_LLM,
    DECISION_WATCH,
    DecisionConfig,
    DecisionEngine,
    MatchDecision,
)
from .llm import LLMClient, LLMNotConfigured, load_pi_config

__all__ = [
    "AnalysisConfig",
    "AnalysisService",
    "build_analysis_service",
]

#: 决策缓存有效期（秒）
DEFAULT_CACHE_TTL_S = 120.0

#: 单次决策分析的最大赛事数（LLM 调用昂贵，须设上限）
DEFAULT_MAX_ANALYZE = 12

#: 无 LLM 时仍可分析，但决策只会是 no_llm（诚实降级）


def _to_int(value: object, default: int = 0) -> int:
    """容错整数转换。

    `AnalysisConfig` 是公开 dataclass，调用方可能传入字符串（如来自环境变量
    或 JSON），直接 `int()` 会抛 TypeError 并让整个分析请求失败。
    """
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default


@dataclass(frozen=True)
class AnalysisConfig:
    """分析层配置。"""

    cache_ttl_s: float = DEFAULT_CACHE_TTL_S
    max_analyze: int = DEFAULT_MAX_ANALYZE
    #: 优先分析的市场（按优先级）；乐鱼源多为 AH/OU
    preferred_markets: Tuple[str, ...] = ("HAD", "1X2", "AH", "AH(0)", "AH(0.25)",
                                          "AH(-0.5)", "OU", "OU(2.5)", "OU(3)")
    #: 是否启用在线的 LLM 分析
    use_llm: bool = True
    #: LLM 分析并发数（过高会打爆推理服务）
    llm_concurrency: int = 2


class AnalysisService:
    """统一分析服务（线程安全）。"""

    def __init__(
        self,
        valuation: Any,
        realtime: Optional[RealtimeHub] = None,
        engine: Optional[DecisionEngine] = None,
        config: Optional[AnalysisConfig] = None,
    ) -> None:
        self.valuation = valuation
        self.realtime = realtime
        self.config = config or AnalysisConfig()
        #: 是否成功接上 LLM（决定决策是否可能是 buy）
        self.llm_error = ""
        self.llm: Optional[LLMClient] = None

        if engine is not None:
            self.engine = engine
        else:
            self.engine = DecisionEngine(
                DecisionConfig(use_llm=self.config.use_llm))
            if self.config.use_llm:
                self._try_attach_llm()

        self._lock = threading.RLock()
        self._cache: Dict[str, Tuple[float, MatchDecision]] = {}
        self._last_run: Optional[Dict[str, Any]] = None
        self._analyze_lock = threading.Lock()

    # -- LLM ---------------------------------------------------------------

    def _try_attach_llm(self) -> None:
        """尝试接上 pi 的模型配置；失败则记录原因并自我降级。"""
        try:
            self.llm = LLMClient(load_pi_config())
            self.engine.attach_llm(self.llm)
            self.llm_error = ""
        except (LLMNotConfigured, Exception) as exc:  # noqa: BLE001 - 配置问题不应让服务起不来
            self.llm = None
            self.llm_error = str(exc)[:300]
            self.engine.attach_llm(None)

    # -- 候选选取 -----------------------------------------------------------

    def _market_for(self, match: Mapping[str, Any]) -> Optional[str]:
        """从赛事可用市场中选一个用于决策的市场（优先胜平负，其次让球/大小）。"""
        markets = match.get("markets") or []
        if not markets:
            return None
        for pref in self.config.preferred_markets:
            if pref in markets:
                return pref
        # 退而求其次：任意 AH/OU，最后取第一个
        for m in markets:
            if isinstance(m, str) and m.startswith(("AH", "OU")):
                return m
        return markets[0] if isinstance(markets[0], str) else None

    def _build_context(self, match: Mapping[str, Any]) -> Dict[str, Any]:
        """从实时 Hub 补充比分/阶段上下文。"""
        ctx: Dict[str, Any] = {"status_text": match.get("state")}
        mid = str(match.get("match_id", ""))
        if self.realtime is None:
            return ctx
        score = self.realtime.score(mid)
        if score:
            ctx["score"] = "%d:%d" % score
        status = self.realtime.status(mid)
        if status:
            if status.get("mst"):
                ctx["minute"] = status["mst"]
            if status.get("mmp"):
                ctx["mmp"] = status["mmp"]
        if self.realtime.is_finished(mid):
            ctx["status_text"] = "已结束"
        return ctx

    def candidates(
        self,
        limit: Optional[int] = None,
        only_live: bool = False,
        league: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """选取待分析的候选赛事。

        优先「进行中」，其次未开赛；已结束的不分析（无决策价值）。
        """
        matches = self.valuation.list_matches(league=league)
        out: List[Dict[str, Any]] = []
        for m in matches:
            st = str(m.get("state") or "")
            if st == "delisted":
                continue
            if only_live and st != "active":
                continue
            mkt = self._market_for(m)
            if mkt is None:
                continue
            m = dict(m)
            m["_market"] = mkt
            out.append(m)

        # 进行中优先（实时价值最高），其余按赔率变动/时间
        live_mids = set(self.realtime.subscribed()) if self.realtime else set()
        def sort_key(m: Mapping[str, Any]) -> Tuple[int, str, str]:
            is_live = 0 if str(m.get("match_id")) in live_mids else 1
            return (is_live, str(m.get("date") or ""), str(m.get("match_id")))
        out.sort(key=sort_key)
        cap = limit if limit is not None else self.config.max_analyze
        return out[: max(0, _to_int(cap))]

    # -- 决策 ---------------------------------------------------------------

    def analyze(
        self,
        match_id: str,
        market: Optional[str] = None,
        force: bool = False,
    ) -> Optional[MatchDecision]:
        """对指定赛事给出决策（带缓存）。"""
        key = "%s|%s" % (match_id, market or "")
        now = time.time()
        if not force:
            with self._lock:
                hit = self._cache.get(key)
                if hit and (now - hit[0]) <= self.config.cache_ttl_s:
                    return hit[1]

        match = self.valuation.get_match(match_id)
        if match is None:
            return None
        mkt = market or self._market_for(match)
        if mkt is None:
            return None

        snap = self.valuation._latest(match_id, mkt, fallback=True)
        if snap is None:
            return None

        trend = self.realtime.trend(match_id) if self.realtime else None
        ctx = self._build_context(match)
        n_markets = len(match.get("markets") or [])

        decision = self.engine.decide(
            snap, trend=trend, context=ctx, n_markets=n_markets)
        with self._lock:
            self._cache[key] = (now, decision)
        return decision

    def decide_list(
        self,
        limit: Optional[int] = None,
        only_live: bool = False,
        league: Optional[str] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """批量决策，返回按优劣排序的列表（供控制台列表直接渲染）。"""
        # 串行化，避免控制台并发刷新触发重复 LLM 调用
        with self._analyze_lock:
            cands = self.candidates(limit=limit, only_live=only_live, league=league)
            t0 = time.time()
            decisions: List[MatchDecision] = []
            errors: List[str] = []

            # 并行分析：LLM 单场耗时 3~10 秒，串行会让 12 场→100 秒以上，
            # 控制台无法接受。并发度受 llm_concurrency 限制，
            # 以免打爆推理服务（它可能排队或限流）。
            workers = (max(1, _to_int(self.config.llm_concurrency, 2))
                       if self.engine.llm_available else min(8, len(cands) or 1))
            if len(cands) <= 1 or workers <= 1:
                for m in cands:
                    mid = str(m.get("match_id"))
                    try:
                        d = self.analyze(mid, market=m.get("_market"), force=force)
                    except Exception as exc:  # noqa: BLE001
                        errors.append("%s: %s" % (mid, exc))
                        continue
                    if d is not None:
                        decisions.append(d)
            else:
                from concurrent.futures import ThreadPoolExecutor, as_completed

                def _one(mm: Mapping[str, Any]) -> Optional[MatchDecision]:
                    return self.analyze(str(mm.get("match_id")),
                                        market=mm.get("_market"), force=force)

                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {pool.submit(_one, m): str(m.get("match_id"))
                               for m in cands}
                    for fut in as_completed(futures):
                        mid = futures[fut]
                        try:
                            d = fut.result()
                        except Exception as exc:  # noqa: BLE001
                            errors.append("%s: %s" % (mid, exc))
                            continue
                        if d is not None:
                            decisions.append(d)

            decisions.sort(key=lambda d: d.rank_score, reverse=True)
            summary = {
                "n": len(decisions),
                "buy": sum(1 for d in decisions if d.decision == DECISION_BUY),
                "watch": sum(1 for d in decisions if d.decision == DECISION_WATCH),
                "avoid": sum(1 for d in decisions if d.decision == DECISION_AVOID),
                "no_llm": sum(1 for d in decisions if d.decision == DECISION_NO_LLM),
            }
            result = {
                "count": len(decisions),
                "summary": summary,
                "llm": self.engine.llm_health(),
                "elapsed_s": round(time.time() - t0, 2),
                "errors": errors[:10],
                "decisions": [d.as_dict() for d in decisions],
            }
            with self._lock:
                self._last_run = {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "elapsed_s": result["elapsed_s"], "summary": summary,
                }
            return result

    # -- 诊断 ---------------------------------------------------------------

    def llm_health(self) -> Dict[str, Any]:
        """LLM 健康（委托给引擎，便于 API 层直接调用）。"""
        h = self.engine.llm_health()
        if self.llm_error:
            h["error"] = self.llm_error
        return h

    def health(self) -> Dict[str, Any]:
        with self._lock:
            cached = len(self._cache)
            last = self._last_run
        out: Dict[str, Any] = {
            "llm_error": self.llm_error,
            "cached_decisions": cached,
            "last_run": last,
            "engine": self.engine.health(),
            "config": {
                "cache_ttl_s": self.config.cache_ttl_s,
                "max_analyze": self.config.max_analyze,
                "use_llm": self.config.use_llm,
            },
        }
        if self.realtime is not None:
            out["realtime"] = self.realtime.health()
        return out

    def clear_cache(self) -> int:
        with self._lock:
            n = len(self._cache)
            self._cache.clear()
        return n


def build_analysis_service(
    valuation: Any,
    realtime: Optional[RealtimeHub] = None,
    env: Optional[Mapping[str, str]] = None,
) -> AnalysisService:
    """按环境变量构造分析服务。

    环境变量：
        ANALYSIS_USE_LLM=0        关闭 LLM（纯小模型，决策为 no_llm）
        ANALYSIS_MAX=12           单次分析赛事上限
        ANALYSIS_CACHE_TTL=120    决策缓存秒数
    """
    e = env if env is not None else os.environ
    use_llm = (e.get("ANALYSIS_USE_LLM", "1") or "1").strip().lower() not in ("0", "false", "no")

    def _num(name: str, default: float) -> float:
        raw = (e.get(name) or "").strip()
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    cfg = AnalysisConfig(
        use_llm=use_llm,
        max_analyze=_to_int(_num("ANALYSIS_MAX", DEFAULT_MAX_ANALYZE),
                            DEFAULT_MAX_ANALYZE),
        cache_ttl_s=_num("ANALYSIS_CACHE_TTL", DEFAULT_CACHE_TTL_S),
    )
    return AnalysisService(valuation=valuation, realtime=realtime, config=cfg)
