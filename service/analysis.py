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
import random
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
from .match_decision import MatchDecisionEngine, MatchPicks
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


#: 默认循环间隔（秒）：每隔这么久重新跑一轮决策。
#: 为何不依赖页面刷新：用户刷新页面不应触发一轮 LLM（慢且浪费）。
#: 后台定时跑，页面只读缓存结果 —— 打开即秒出。
#:
#: 取值依据（实测，并发 4）：单场中位 23s、最坏 90s（硬超时）。
#: 20 场/轮约 2~3 分钟，因此间隔至少设 180s（否则循环会持续落后）。
DEFAULT_CYCLE_INTERVAL_S = 300.0

#: 每轮决策的赛事数上限。
#: 不宜过大：一轮要跑完全部 LLM 调用才能更新缓存，
#: 场次越多则缓存刷新越慢（用户看到的决策越旧）。
DEFAULT_CYCLE_LIMIT = 12

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
    #: LLM 分析并发数（**多场并行**）。
    #: 实测推理服务可承受 4 并发（4 个请求总耗时 4.4s，而非串行的 9s），
    #: 调高能显著缩短整批决策时间；过高可能被服务端排队或限流。
    llm_concurrency: int = 4
    #: 买入门槛：优势低于此值不给买入建议
    min_edge: float = 0.02
    #: 买入门槛：LLM 置信度低于此值只观望
    min_confidence: float = 0.5
    #: 后台定时决策间隔（秒）；0 表示不启用定时
    cycle_interval_s: float = DEFAULT_CYCLE_INTERVAL_S
    #: 每轮决策赛事数上限
    cycle_limit: int = DEFAULT_CYCLE_LIMIT
    #: 是否只对进行中的赛事做定时决策（节省 LLM 开销）
    cycle_live_only: bool = False
    #: 决策结果落盘路径（重启后仍能立即展示上次结果）
    result_path: Optional[str] = None



class AnalysisService:
    """统一分析服务（线程安全）。

    主路径是**盘口汇总式**决策（`MatchDecisionEngine`）：
    经济学算法先算完一场的全部盘口，再汇总给 LLM 做**一次**买入裁定。

    ## 决策触发方式（后台定时，非页面刷新）

    * `start_cycle()` 启动**后台定时循环**：每 `cycle_interval_s` 秒自动
      重跑一轮决策，结果存入内存缓存 + 落盘快照。
    * 页面/API 只需读 `latest_result()`（毫秒级，不触发 LLM）。
    * `start_job()` 仍保留，用于手动强制刷新（但要避免滥用）。

    为什么必须定时而不是按需：LLM 一轮要数十秒，若等用户刷新才跑，
    用户要对着转圈等很久，且每个人都重复触发一遍（浪费且易限流）。
    """

    def __init__(
        self,
        valuation: Any,
        realtime: Optional[RealtimeHub] = None,
        engine: Optional[Any] = None,
        config: Optional[AnalysisConfig] = None,
    ) -> None:
        self.valuation = valuation
        self.realtime = realtime
        self.config = config or AnalysisConfig()
        #: 是否成功接上 LLM（决定决策是否可能是 buy）
        self.llm_error = ""
        self.llm: Optional[LLMClient] = None

        dcfg = DecisionConfig(
            use_llm=self.config.use_llm,
            min_edge=self.config.min_edge,
            min_confidence=self.config.min_confidence,
        )
        #: 汇总式引擎（主路径）：经济学算全部盘口 → 一次 LLM 裁定
        self.match_engine: MatchDecisionEngine = (
            MatchDecisionEngine(dcfg) if engine is None else engine)
        #: 逐盘口引擎（详情页/深度分析用）
        self.engine = DecisionEngine(dcfg)
        if self.config.use_llm:
            self._try_attach_llm()

        self._lock = threading.RLock()
        self._cache: Dict[str, Tuple[float, Any]] = {}
        self._last_run: Optional[Dict[str, Any]] = None
        self._analyze_lock = threading.Lock()
        #: 异步任务表 job_id → 状态
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._jobs_lock = threading.RLock()
        #: 后台定时决策（主路径）：不依赖页面刷新
        self._cycle_thread: Optional[threading.Thread] = None
        self._cycle_stop = threading.Event()
        self.cycle_stats: Dict[str, Any] = {
            "rounds": 0, "last_started": 0.0, "last_finished": 0.0,
            "last_elapsed_s": 0.0, "last_error": "", "next_at": 0.0,
        }
        #: 最近一轮结果（页面只读它，**不触发 LLM**）
        self._latest: Optional[Dict[str, Any]] = None
        self._restore_latest()

    # -- 后台定时决策（主路径） -------------------------------------------

    def start_cycle(self) -> bool:
        """启动后台定时决策循环（幂等）。返回是否实际启动。"""
        if self._cycle_thread is not None and self._cycle_thread.is_alive():
            return False
        if self.config.cycle_interval_s <= 0:
            return False
        self._cycle_stop.clear()
        self._cycle_thread = threading.Thread(
            target=self._cycle_loop, name="analysis-cycle", daemon=True)
        self._cycle_thread.start()
        return True

    def stop_cycle(self, timeout: float = 5.0) -> None:
        self._cycle_stop.set()
        t = self._cycle_thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)

    @property
    def cycle_running(self) -> bool:
        return self._cycle_thread is not None and self._cycle_thread.is_alive()

    def _cycle_loop(self) -> None:
        """定时跑决策。首轮立即执行（不等一个间隔），让系统尽快有数据。"""
        interval = max(10.0, float(self.config.cycle_interval_s))
        while not self._cycle_stop.is_set():
            self._run_cycle_once()
            self.cycle_stats["next_at"] = time.time() + interval
            if self._cycle_stop.wait(interval):
                return

    def _run_cycle_once(self) -> None:
        """执行一轮决策并缓存结果。异常只记录，不让循环退出。"""
        self.cycle_stats["last_started"] = time.time()
        try:
            res = self.decide_list(
                limit=self.config.cycle_limit,
                only_live=self.config.cycle_live_only,
                force=True,
                max_workers=self.config.llm_concurrency,
            )
            res["cycle"] = True
            res["finished_at"] = datetime.now(timezone.utc).isoformat()
            with self._lock:
                self._latest = res
            self._persist_latest(res)
            self.cycle_stats["rounds"] += 1
            self.cycle_stats["last_error"] = ""
        except Exception as exc:  # noqa: BLE001 - 定时循环不能因单轮失败而退出
            self.cycle_stats["last_error"] = "%s: %s" % (type(exc).__name__, exc)
        finally:
            self.cycle_stats["last_finished"] = time.time()
            self.cycle_stats["last_elapsed_s"] = round(
                self.cycle_stats["last_finished"]
                - self.cycle_stats["last_started"], 2)

    # -- 结果持久化（重启后立即有数据可看） -------------------------------

    def _persist_latest(self, res: Mapping[str, Any]) -> None:
        """把一轮结果落盘。失败只记录，不影响服务。"""
        path = self.config.result_path
        if not path:
            return
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(res, fh, ensure_ascii=False)
            os.replace(tmp, path)  # 原子替换，避免读到半写文件
        except (OSError, ValueError, TypeError) as exc:
            self.cycle_stats["last_error"] = "落盘失败: %s" % exc

    def _restore_latest(self) -> None:
        """启动时读回上次结果，使页面在首轮完成前也能看到数据。"""
        path = self.config.result_path
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "decisions" in data:
                data["restored"] = True
                with self._lock:
                    self._latest = data
        except (OSError, ValueError):
            pass  # 文件损坏就当没有，等下一轮重新生成

    def latest_result(self, max_age_s: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """读最近一轮决策结果（**毫秒级，不触发 LLM**）。

        这是页面应该调用的接口：打开即出结果，不转圈。

        Args:
            max_age_s: 超过该年龄则返回 None（调用方可据此提示“数据较旧”）。
        """
        with self._lock:
            res = self._latest
        if res is None:
            return None
        if max_age_s is not None:
            fin = res.get("finished_at")
            if fin:
                try:
                    age = (datetime.now(timezone.utc)
                           - datetime.fromisoformat(fin)).total_seconds()
                    if age > max_age_s:
                        return None
                except (TypeError, ValueError):
                    pass
        return res

    # -- 异步任务 -----------------------------------------------------------

    def start_job(
        self,
        limit: int = 8,
        only_live: bool = False,
        league: Optional[str] = None,
        max_workers: Optional[int] = None,
    ) -> str:
        """启动后台决策任务，**立即返回** job_id。

        为什么需要：LLM 决策耗时数十秒，同步 HTTP 会 504。
        前端拿 job_id 后轮询 `/decisions/job/<id>` 看进度。
        """
        job_id = "job-%d-%04x" % (int(time.time()), random.getrandbits(16))
        with self._jobs_lock:
            self._jobs[job_id] = {
                "id": job_id, "state": "running", "started_at": time.time(),
                "done": 0, "total": 0, "result": None, "error": "",
                "limit": limit, "only_live": only_live, "league": league,
            }
            # 只保留最近 10 个任务，防无界增长
            if len(self._jobs) > 10:
                for k in sorted(self._jobs,
                                key=lambda kk: self._jobs[kk]["started_at"])[:-10]:
                    self._jobs.pop(k, None)

        def _run() -> None:
            try:
                res = self.decide_list(
                    limit=limit, only_live=only_live, league=league,
                    force=True, max_workers=max_workers, job_id=job_id,
                )
                with self._jobs_lock:
                    self._jobs[job_id].update(state="done", result=res)
            except Exception as exc:  # noqa: BLE001 - 任务异常要能回报给前端
                with self._jobs_lock:
                    self._jobs[job_id].update(state="failed", error=str(exc)[:300])

        threading.Thread(target=_run, name=job_id, daemon=True).start()
        return job_id

    def job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._jobs_lock:
            j = self._jobs.get(job_id)
            if j is None:
                return None
            out = {k: v for k, v in j.items() if k != "result"}
            out["elapsed_s"] = round(time.time() - j["started_at"], 1)
            if j["state"] == "done":
                out["result"] = j["result"]
            return out

    def _set_job_progress(self, job_id: Optional[str], done: int,
                          total: int) -> None:
        if not job_id:
            return
        with self._jobs_lock:
            j = self._jobs.get(job_id)
            if j is not None:
                j["done"] = done
                j["total"] = total

    # -- LLM ---------------------------------------------------------------

    def _try_attach_llm(self) -> None:
        """尝试接上 pi 的模型配置；失败则记录原因并自我降级。"""
        try:
            self.llm = LLMClient(load_pi_config())
            self.match_engine.attach_llm(self.llm)
            self.engine.attach_llm(self.llm)
            self.llm_error = ""
        except (LLMNotConfigured, Exception) as exc:  # noqa: BLE001 - 配置问题不应让服务起不来
            self.llm = None
            self.llm_error = str(exc)[:300]
            self.match_engine.attach_llm(None)
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
        max_workers: Optional[int] = None,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """批量决策（**盘口汇总式**），返回按优劣排序的列表。

        每场只调 **1 次** LLM：先把该场全部盘口交给经济学算法算完，
        再把汇总结果一次性交给 LLM 做买入裁定。
        （旧实现逐盘口调 LLM，一场平均 14.5 个盘口 → 6 场 300s+ → 504）

        Args:
            max_workers: 并行度；默认取 `llm_concurrency`。
            job_id: 异步任务 id（用于回报进度）。
        """
        with self._analyze_lock:
            cands = self.candidates(limit=limit, only_live=only_live, league=league)
            t0 = time.time()
            self.stats_runs = getattr(self, "stats_runs", 0) + 1

            # 组装每场的输入：全部快照 + 走势 + 上下文
            items: List[Tuple[str, str, str, str, List[Any], Any, Dict[str, Any]]] = []
            for m in cands:
                mid = str(m.get("match_id"))
                snaps = self.valuation._snapshots_of(mid)
                if not snaps:
                    continue
                trend = self.realtime.trend(mid) if self.realtime else None
                ctx = self._build_context(m)
                items.append((mid, str(m.get("league") or ""),
                              str(m.get("home") or ""), str(m.get("away") or ""),
                              snaps, trend, ctx))

            self._set_job_progress(job_id, 0, len(items))

            workers = (max(1, _to_int(max_workers or self.config.llm_concurrency, 4))
                       if self.match_engine.llm_available
                       else min(8, len(items) or 1))
            # 带进度回报的并行（每完成一场累加 done）
            results: List[Any] = []
            if len(items) <= 1 or workers <= 1:
                for i, it in enumerate(items):
                    try:
                        results.append(self.match_engine.decide_match(
                            it[4], trend=it[5], context=it[6]))
                    except Exception as exc:  # noqa: BLE001
                        results.append(self._failed_pick(it[0], exc))
                    self._set_job_progress(job_id, i + 1, len(items))
            else:
                from concurrent.futures import ThreadPoolExecutor, as_completed

                def _one(it: Any) -> Any:
                    return self.match_engine.decide_match(
                        it[4], trend=it[5], context=it[6])

                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futs = {pool.submit(_one, it): it for it in items}
                    done = 0
                    for fut in as_completed(futs):
                        it = futs[fut]
                        try:
                            results.append(fut.result())
                        except Exception as exc:  # noqa: BLE001
                            results.append(self._failed_pick(it[0], exc))
                        done += 1
                        self._set_job_progress(job_id, done, len(items))

            results.sort(key=lambda r: r.rank_score, reverse=True)
            errors = ["%s: %s" % (r.match_id, r.error) for r in results if r.error]
            summary = {
                "n": len(results),
                "buy": sum(1 for r in results if r.has_buy),
                "no_llm": sum(1 for r in results
                              if r.decision == DECISION_NO_LLM),
                "avoid": sum(1 for r in results
                             if r.decision == DECISION_AVOID),
                "n_picks": sum(len(r.picks) for r in results),
            }
            result = {
                "count": len(results),
                "summary": summary,
                "llm": self.match_engine.llm_health(),
                "elapsed_s": round(time.time() - t0, 2),
                "errors": errors[:10],
                "decisions": [r.as_dict() for r in results],
            }
            with self._lock:
                self._last_run = {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "elapsed_s": result["elapsed_s"], "summary": summary,
                }
            return result

    @staticmethod
    def _failed_pick(match_id: str, exc: Exception) -> MatchPicks:
        """构造一个表示失败的 MatchPicks（单场失败不应中断整批）。"""
        r = MatchPicks(match_id=match_id)
        r.decision = DECISION_AVOID
        r.error = "%s: %s" % (type(exc).__name__, exc)
        return r

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
        # 定时决策（默认 180s）；ANALYSIS_CYCLE=0 可关闭
        cycle_interval_s=_num("ANALYSIS_CYCLE", DEFAULT_CYCLE_INTERVAL_S),
        cycle_limit=_to_int(_num("ANALYSIS_CYCLE_LIMIT", DEFAULT_CYCLE_LIMIT),
                            DEFAULT_CYCLE_LIMIT),
        cycle_live_only=(e.get("ANALYSIS_CYCLE_LIVE_ONLY", "") or "").strip()
                        in ("1", "true", "yes"),
        result_path=(e.get("ANALYSIS_RESULT_PATH") or "").strip() or None,
    )
    return AnalysisService(valuation=valuation, realtime=realtime, config=cfg)
