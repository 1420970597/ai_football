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

import inspect
import json
import math
import os
import copy
import random
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, cast

from collector.leyu_normalizer import (
    snapshots_from_live,
    snapshots_from_match,
)
from collector.leyu_realtime import RealtimeHub
from collector.sources import SOCCER_SPORT_ID
from store.history import HistoryJournal
from core.live_model import competition_type

from .decision import (
    DECISION_AVOID,
    DECISION_NO_LLM,
    DecisionConfig,
    DecisionEngine,
    MatchDecision,
)
from .match_decision import (
    MAX_MARKETS_PER_PROMPT,
    MatchDecisionEngine,
    MatchPicks,
)
from .ledger import DecisionLedger
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

#: 比分推送新鲜度上限（秒）；超过即判该场不活跃（见 AnalysisConfig.stale_score_s）
DEFAULT_STALE_SCORE_S = 300.0

#: 推送行情的新鲜度上限（秒）：超过即**不得**算作「正在进行中」。
#:
#: 为何需要（本项目真实故障）：`LiveBook` 会把内存实时表落盘，启动时回填。
#: 早期回填把每行都标成「刚写入」，于是 15~24 小时前的旧行情在
#: `/api/v1/analysis` 里显示成 `count=527, source=push`（自称「真实进行中」），
#: 而当日实际滚球只有几十场。这与 HANDOVER §6.3 「用快照时效冒充进行中」
#: 是同一类错误，只是从「快照库」跑到了「实时表回填」这条路径上。
#:
#: 取值与 `collector.leyu_realtime.DEFAULT_QUOTE_MAX_AGE_S` 保持一致：
#: `C105` 是周期性全量快照，真在滚球的场次不会 15 分钟没有新价。
DEFAULT_PUSH_QUOTE_MAX_AGE_S = 900.0

#: 已开赛且从无比分推送的最大容忍时长（秒），见 max_live_age_s。
#: 取 150 分钟：覆盖加时/点球，又足以排除“几天前的旧快照”。
DEFAULT_MAX_LIVE_AGE_S = 9000.0

#: 单场 LLM 决策硬超时（秒）。
#:
#: 实测默认 90s 时，一轮 71 场中有 **11 场**因超时降级为 `no_llm`
#: （全部报“LLM 决策超时（>90s）”）。单场平均耗时 28.85s，
#: 但推理型模型在长 prompt 上尾部很长；150s 覆盖实测绝大多数尾部。
DEFAULT_LLM_TIMEOUT_S = 150.0

#: LLM 概率相对市场的最大允许偏离（污染护栏，见 DecisionConfig）。
DEFAULT_MAX_PROB_DEVIATION = 0.35

#: 无 LLM 时仍可分析，但决策只会是 no_llm（诚实降级）


def _start_epoch_s(match: Mapping[str, Any]) -> Optional[float]:
    """从 `/matches` 的日期/时间字段推算开赛的 epoch 秒；缺失返回 None。

    为何不用 `time.time()` 兜底："没有开赛时间" 与 "刚刚开赛" 语义
    完全不同 —— 前者无法判断，应该交给调用方回退到 `is_live`。
    """
    date_s = str(match.get("date") or "").strip()
    time_s = str(match.get("time") or "").strip()
    if not date_s:
        return None
    txt = ("%s %s" % (date_s, time_s)).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(txt, fmt)
        except ValueError:
            continue
        # 上游给的是北京时间（本项目容器 TZ=Asia/Shanghai），
        # 统一转为 UTC 后参与比较；无时区信息时按北京时间处理。
        return dt.replace(tzinfo=timezone(timedelta(hours=8))).timestamp()
    return None


def _to_float(value: object, default: float = 0.0) -> float:
    """容错浮点转换。

    `AnalysisConfig` 是公开 dataclass，调用方可能（经环境变量/JSON）
    传入字符串；`float()` 抛错会让定时循环线程直接挂掉。
    """
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def _to_int(value: object, default: int = 0) -> int:
    """容错整数转换。

    `AnalysisConfig` 是公开 dataclass，调用方可能传入字符串（如来自环境变量
    或 JSON），直接 `int()` 会抛 TypeError 并让整个分析请求失败。
    """
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError, OverflowError):
        return default


#: 默认循环间隔（秒）：每隔这么久重新跑一轮决策。
#: 为何不依赖页面刷新：用户刷新页面不应触发一轮 LLM（慢且浪费）。
#: 后台定时跑，页面只读缓存结果 —— 打开即秒出。
#:
#: 取值依据（实测，并发 4）：单场中位 23s、最坏 90s（硬超时）。
#: 60 场/轮在并发 4 下约 6~10 分钟，因此间隔设为 600s；
#: 且**只对进行中的赛事**决策（未开赛的盘口还在变，早算无意义）。
DEFAULT_CYCLE_INTERVAL_S = 600.0

#: 每轮决策的赛事数上限。
#: **0 表示不限制** —— 应覆盖全部进行中赛事。
#: 用户要求：展示应与乐鱼接口的进行中数量一致（实测足球 ~50 场，
#: 但不同时段会波动，写死 60 早晚会不够）。
#: 真正的约束是 LLM 耗时，而不是一个人为的场次上限。
DEFAULT_CYCLE_LIMIT = 0

#: 盘口变动触发决策的**单场静默窗口**（秒）。
#:
#: 语义：某场第一次观测到盘口变动后，等 `CHANGE_DEBOUNCE_S` 秒再决策。
#: 期间若又有变动，则把该场重新排队（计时重置）。
#:
#: 为何需要：实测单个盘口在进球/红牌后会**连跳十几次**，逐跳触发
#: 会让同一场在几秒内被反复决策（LLM 一轮 20s+，纯属浪费且必然超时）。
#: 快路径使用 150ms 合并窗口，连续变动不会推迟首个到期时间。
DEFAULT_CHANGE_DEBOUNCE_S = 0.15

#: 两轮决策之间的**全局最小间隔**（秒），所有赛事共享。
#:
#: 为何需要：行情活跃时可能有 40+ 场同时变动；若全部并发触发，
#: 会瞬间打满 LLM 配额（实测推理服务 4 并发即饱和）。
#: 快路径不调用 LLM，50ms 间隔限制批处理唤醒频率。
DEFAULT_CHANGE_MIN_INTERVAL_S = 0.05

#: 触发式决策每轮最多处理多少场（按变动时间先后）。
#: 剩余赛事保留在待办队列里，下一轮继续 —— 不丢，只是排队。
DEFAULT_CHANGE_BATCH = 12

#: 待办队列长度上限（防止行情暴涨时无限堆积）。
#: 超出时丢弃**最久未变动**的赛事（它们最可能已经不再有价值）。
DEFAULT_CHANGE_QUEUE_MAX = 200

#: 自适应限流：是否根据**实测 LLM 吞吐**调节批量与间隔。
#:
#: 为何需要（HANDOVER §3.4 实测）：固定 `change_batch=12` + 固定 20s 间隔，
#: 遇到 LLM 慢时（单批 12 场 × 150s）待办队列会涨到 72 场；
#: 遇到 LLM 快时又白等 20s。让批大小跟随实测耗时更贴近真实吞吐。
DEFAULT_ADAPTIVE_THROTTLE = True

#: 自适应目标：单批耗时尽量不超过这个值（秒）。
#: 快路径目标批处理预算为 100ms；
#: 调小更跟手但批更小（周转更频繁），调大则相反。
DEFAULT_BATCH_TARGET_S = 0.1

#: 自适应批量的上下限（防止离群值把批量推到无意义的两端）
DEFAULT_BATCH_MIN = 3
DEFAULT_BATCH_MAX = 24


def _live_mids_supports_max_age(book: Any) -> bool:
    """`book.live_mids` 是否支持 `max_age_s` 关键字（能力探测）。

    为何用签名探测而不是 `try/except TypeError`：把「能力探测」与
    「调用出错」混进同一个 `except` 会**吞掉真实缺陷** —— 任何来自
    实现内部的 TypeError 都会被误判成「旧版不支持」而静默转走兜底分支
    （本项目已有同类教训，见 HANDOVER §6.6）。此处只问签名，不动数据。

    Returns:
        接受 `max_age_s`（显式命名参数或 `**kwargs`）时为 True。
    """
    fn = getattr(book, "live_mids", None)
    if not callable(fn):
        return False
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):  # C 实现/签名不可用时保守处理
        return False
    if "max_age_s" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD
               for p in params.values())


@dataclass(frozen=True)
class AnalysisConfig:
    """分析层配置。"""

    fast_live: bool = True
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
    #: 50 场进行中赛事在并发 4 下约 6~10 分钟一轮。
    llm_concurrency: int = 4
    #: 买入门槛：优势低于此值不给买入建议
    min_edge: float = 0.02
    #: 买入门槛：LLM 置信度低于此值只观望。
    #:
    #: ⚠️ 必须与提示词的语义一致（本项目真实故障）：
    #: 提示词要求「仅有赔率时给低值(<0.5)」，而原值为 0.5 → 两者矛盾，
    #: 实测 LLM 自评置信度中位 0.33、达标率 0%，导致**永远零买入建议**。
    #: 决策变量是 edge；置信度只用于过滤「模型自己都没把握」的输出。
    min_confidence: float = 0.25
    #: 后台定时决策间隔（秒）；0 表示不启用定时
    cycle_interval_s: float = DEFAULT_CYCLE_INTERVAL_S
    #: 每轮决策赛事数上限
    cycle_limit: int = DEFAULT_CYCLE_LIMIT
    #: 是否只对进行中的赛事做定时决策（节省 LLM 开销）
    #: 默认 True：未开赛赛事盘口仍在变化，过早决策无意义且浪费 LLM。
    cycle_live_only: bool = True
    #: 是否启用「盘口变动自动触发决策」
    #:
    #: 这是用户明确要求的触发时机：赔率/盘口一变就重算该场，
    #: 而不是死等 `cycle_interval_s`。定时循环降级为**兜底**
    #: （覆盖「长时间无变动但需要刷新」与推送断线的情况）。
    change_trigger: bool = True
    #: 分析时**不下发**当前比分/比赛时钟给 LLM。
    #:
    #: 为什么必须关掉（本项目真实故障）：比分一旦进入提示词，LLM 会直接
    #: “抄答案”——实测多条买入建议的理由原文是「终场0:0，小球」
    #: 「主队2:0取胜，平手盘主胜」，并给出 p_llm=1.0 而市场仅 0.35
    #: 的虚假 edge（均值 +42%）。这既制造假信号，又让赛后无法评估
    #: LLM 真实水平（等于把答案泄给了考生）。
    #: 只保留开赛/结束状态（trading 语义），比分与分钟数一律不传。
    leak_score_to_llm: bool = False
    #: 比分推送的新鲜度上限（秒）。
    #:
    #: 超过该时长仍未收到比分推送，即认为该场**已不再活跃**，
    #: 即便赛程接口暂时失败、快照 state 仍为 active，也不送去做决策。
    #: 依据：进行中比赛的比分（C103）是秒级推送的（实测全量
    #: `score_updates` 数万条、两小时内 `idle_s` < 2s），
    #: 因此「5 分钟没有比分更新」是一个很保守的过期阈值。
    stale_score_s: float = DEFAULT_STALE_SCORE_S
    #: 推送行情新鲜度上限（秒）；超过即不算「进行中」（见上面常量注释）
    push_quote_max_age_s: float = DEFAULT_PUSH_QUOTE_MAX_AGE_S
    #: 单场 LLM 决策硬超时（秒），见 `DecisionConfig.llm_timeout_s`
    llm_timeout_s: float = DEFAULT_LLM_TIMEOUT_S
    #: LLM 概率相对市场的最大偏离（污染护栏），见 `DecisionConfig`
    max_prob_deviation: float = DEFAULT_MAX_PROB_DEVIATION
    #: 单场提示词包含的最大盘口数（控 prompt 长度与耗时）
    max_markets_per_prompt: int = 60
    #: 兜底：比赛已开赛超过该时长且从未收到比分推送，视为不活跃。
    #:
    #: 为何需要（真实故障场景）：赛程接口失败时 `live_match_ids()` 返回
    #: None，`is_live` 会退化成快照的 `state`；而快照 state 只看时效，
    #: 一个几天前入库的**已结束**赛事文件仍是 active，于是被当作
    #: 进行中送去分析，LLM 又能看到终场比分。长时间从无比分推送
    #: 恰恰说明该场不是真在踢。
    max_live_age_s: float = DEFAULT_MAX_LIVE_AGE_S
    #: 单场防抖窗口（秒），见 `DEFAULT_CHANGE_DEBOUNCE_S`
    change_debounce_s: float = DEFAULT_CHANGE_DEBOUNCE_S
    #: 全局最小触发间隔（秒），见 `DEFAULT_CHANGE_MIN_INTERVAL_S`
    change_min_interval_s: float = DEFAULT_CHANGE_MIN_INTERVAL_S
    #: 触发式决策每批场次上限，见 `DEFAULT_CHANGE_BATCH`
    change_batch: int = DEFAULT_CHANGE_BATCH
    #: 按实测 LLM 吞吐自适应调节批量/间隔，见 `DEFAULT_ADAPTIVE_THROTTLE`
    adaptive_throttle: bool = DEFAULT_ADAPTIVE_THROTTLE
    #: 自适应单批耗时目标（秒），见 `DEFAULT_BATCH_TARGET_S`
    batch_target_s: float = DEFAULT_BATCH_TARGET_S
    #: 自适应批量的下限/上限，见 `DEFAULT_BATCH_MIN` / `DEFAULT_BATCH_MAX`
    batch_min: int = DEFAULT_BATCH_MIN
    batch_max: int = DEFAULT_BATCH_MAX
    #: 待办队列上限，见 `DEFAULT_CHANGE_QUEUE_MAX`
    change_queue_max: int = DEFAULT_CHANGE_QUEUE_MAX
    #: 决策结果落盘路径（重启后仍能立即展示上次结果）
    result_path: Optional[str] = None
    #: 决策台账目录（记录「算法说了什么」以备赛后核对）
    ledger_root: Optional[str] = None
    #: 自动结算间隔（秒）；0 表示不启用后台结算
    settle_interval_s: float = 60.0


class AnalysisService:
    """统一分析服务（线程安全）。

    主路径是**盘口汇总式**决策（`MatchDecisionEngine`）：
    经济学算法先算完一场的全部盘口，再汇总给 LLM 做**一次**买入裁定。

    ## 决策触发方式（盘口变动驱动 + 定时兜底）

    * **主路径：盘口变动触发**（`change_trigger=True`）。
      `RealtimeHub` 每次发现**真实赔率变动**就回调 `notify_price_change()`，
      本类按场防抖（`change_debounce_s`）后排队，后台线程按
      `change_min_interval_s` 节流逐批决策。这才是「盘口一变就重算」。
    * **兜底：定时循环**（`start_cycle()`）。行情长时间不动、或推送断线时，
      仍每 `cycle_interval_s` 秒全量重跑一轮，保证页面不会一直陈旧。
      定时循环在触发式调度器存活时**降低频率**（见 `_cycle_loop`）。
    * 页面/API 只需读 `latest_result()`（毫秒级，不触发 LLM）。
    * `start_job()` 仍保留，用于手动强制刷新。

    为何必须后台跑而不是按需：LLM 一轮要数十秒，若等用户刷新才跑，
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
        from service.live_expert import LiveExpertService
        self.live_expert = LiveExpertService()
        self._config = config or AnalysisConfig()
        #: 是否成功接上 LLM（决定决策是否可能是 buy）
        self.llm_error = ""
        self.llm: Optional[LLMClient] = None

        dcfg = DecisionConfig(
            use_llm=self.config.use_llm,
            min_edge=self.config.min_edge,
            min_confidence=self.config.min_confidence,
            # 超时与 prompt 规模从分析层透传，便于用环境变量调优：
            # 实测默认 90s 会让 11/71 场因超时降级为 no_llm。
            llm_timeout_s=_to_float(self.config.llm_timeout_s,
                                    DEFAULT_LLM_TIMEOUT_S),
            max_prob_deviation=_to_float(self.config.max_prob_deviation,
                                         DEFAULT_MAX_PROB_DEVIATION),
            max_markets_per_prompt=_to_int(self.config.max_markets_per_prompt,
                                           MAX_MARKETS_PER_PROMPT),
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
        self._decision_history: Optional[HistoryJournal] = None
        self._decision_history_pending: List[Mapping[str, Any]] = []
        self._history_lock = threading.RLock()
        #: 进行中赛事缓存：`(取时时刻, mid 集合)`。
        #: 命中与否可观测（`_live_source`/`_live_error`），便于定位
        #: 「leyu 67 场 vs 系统 34 场」这类覆盖差异到底来自哪条数据源。
        self._live_cache: Optional[Tuple[float, Optional[set]]] = None
        #: 同一赛程按真实/虚拟/未知拆分，供工作台按类型对账。
        self._live_type_cache: Optional[Tuple[float, Dict[str, set]]] = None
        self._live_source: str = ""
        self._live_error: str = ""
        #: `mid → 名称行` 的字典缓存（`match_index()` 结果是列表，
        #: 逐场线性查找会变成 O(n×m)，见 `_names_dict`）
        self._names_cache: Optional[Tuple[Any, Dict[str, Dict[str, Any]]]] = None
        #: 上游“滚动球”计数缓存 `(取时时刻, {upstream, derived})`。
        #: 用于用户问题 1 的对账（“要与乐鱼一致”必须有同源参照值），
        #: 带 60s TTL 以免每次 `/analysis` 都请求上游。
        self._upstream_count_cache: Optional[Tuple[float, Dict[str, Any]]] = None
        #: 决策台账：记录每个建议与被拦截盘口，供赛后核对 LLM 准确度
        self.ledger = DecisionLedger(self.config.ledger_root)        #: 结算线程（定期把已结束赛事的比分回填进台账）
        self._settle_thread: Optional[threading.Thread] = None
        self._settle_stop = threading.Event()
        self._settle_wake = threading.Event()
        self._settle_run_lock = threading.Lock()
        self.settle_stats: Dict[str, Any] = {
            "rounds": 0, "settled": 0, "void": 0,
            "last_at": 0.0, "last_error": "",
        }
        #: 变动触发式调度器（用户要求的触发时机）
        self._sched_lock = threading.RLock()
        self._sched_thread: Optional[threading.Thread] = None
        self._sched_stop = threading.Event()
        self._sched_wake = threading.Event()
        #: mid → 该场最早可决策的时间（防抖计时）
        self._pending: Dict[str, float] = {}
        self._pending_first: Dict[str, float] = {}
        #: 本批已处理过的赛事（避免同一批重复入 LLM）
        self._sched_stats: Dict[str, Any] = {
            "signals": 0,        # 收到的变动信号数
            "triggered": 0,      # 实际发起决策的赛事数
            "batches": 0,        # 批次数
            "coalesced": 0,      # 被防抖合并掉的信号数
            "dropped": 0,        # 因队列满被丢弃的赛事数
            "last_trigger_at": 0.0,
            "last_trigger_mids": [],
            #: 实测单场耗时（指数滑动平均）——自适应限流的唯一依据
            "per_match_s": 0.0,
            "last_batch_n": 0,
            "last_batch_s": 0.0,
        }
        self._restore_latest()
        from service.runtime_settings import RuntimeConfig, RuntimeSettings
        from service.live_review import LiveReview
        runtime_cfg = RuntimeConfig()
        if self.llm is not None:
            runtime_cfg = replace(runtime_cfg, llm_base_url=self.llm.config.base_url,
                                  llm_model=self.llm.config.model, llm_api_key=self.llm.config.api_key or "")
        self.runtime_settings = RuntimeSettings(runtime_cfg)
        self.live_review = LiveReview()
        from service.betting import BettingExecutor
        self.betting = BettingExecutor(self.runtime_settings, lambda: self.realtime, lambda: self.ledger,
                                       on_recompute=self.notify_price_change)
        from service.data_model import DataModelService
        self.data_model = DataModelService()
        self._bind_runtime_settings()

    def _apply_runtime_settings(self, cfg: Any, version: int) -> None:
        self.live_expert.configure(cfg, version)
        self.live_review.configure(cfg, version)
        self.data_model.configure(cfg)
        self.betting.configure()
        if cfg.betting_enabled:
            self.betting.start()
        else:
            self.betting.stop()
        with self._lock:
            self._cache.clear()
        subscribed = getattr(self.realtime, "subscribed", None)
        if callable(subscribed):
            self.notify_price_change(subscribed())

    def _bind_runtime_settings(self) -> None:
        self.runtime_settings.bind(self.config.ledger_root)
        self.betting.bind(self.config.ledger_root)
        self.data_model.bind(self.config.ledger_root)
        self.live_expert.on_decision = self.ledger.record_live if self.ledger.enabled else None
        self.live_expert.journal_root = self.config.ledger_root
        self.live_expert.update_evidence(self.ledger.performance_evidence())
        self.live_review.on_experiment = self.ledger.record_experiment if self.ledger.enabled else None
        self.live_review.bind_history(self.config.ledger_root)
        self._apply_runtime_settings(self.runtime_settings.config, self.runtime_settings.version)

    def update_settings(self, patch: Mapping[str, Any], version: Any) -> Dict[str, Any]:
        self.runtime_settings.update(patch, version, self._apply_runtime_settings)
        return self.settings_status()

    def settings_status(self) -> Dict[str, Any]:
        out = self.runtime_settings.public()
        out["capabilities"]["betting"] = self.betting.health()
        return out

    # -- 配置（支持运行期注入 ledger_root，且台账会跟着换） -------------------

    @property
    def config(self) -> AnalysisConfig:
        """分析层配置。

        为何要做成属性而不是普通字段（本项目真实故障）：
        `api/app.py` 在构造完本服务后才注入 `ledger_root` /
        `result_path`（它需要先拿到快照根目录才能算输出路径）。
        而台账是在 `__init__` 里根据**当时的**配置创建的 ——
        等到 API 层用 `dataclasses.replace` 把 `ledger_root` 补上，
        `self.ledger` 早已是不可写的空实现：表现为
        启动日志“赛后结算未启用（台账不可用）”、`/ledger/stats` 永远
        零条记录、用户问“LLM 准不准”时根本没有数据可查。

        这里把 config 做成属性，写入时若 `ledger_root` 变化就**重建台账**，
        使“后注入配置”也能生效。
        """
        return self._config

    @config.setter
    def config(self, value: AnalysisConfig) -> None:
        old = getattr(self, "_config", None)
        self._config = value
        old_root = getattr(old, "ledger_root", None) if old else None
        new_root = getattr(value, "ledger_root", None)
        if old is None or old_root != new_root:
            # 重建台账（保留已有对象则改用新路径；已有的落盘数据仍在磁盘）
            self.ledger = DecisionLedger(new_root)
            if hasattr(self, "runtime_settings"):
                self._bind_runtime_settings()
        # ⚠️ `result_path` 变化时必须**重新读回上次结果**。
        #
        # 为何必需（用户报“加载不出来比赛场次”的直接根因）：
        # `api/app.py` 需要先拿到快照根目录才能算出输出路径，因此是
        # **先** `build_analysis_service()`（此刻 `result_path=None`）
        # **后**才 `replace(config, result_path=…)` 注入。
        # 而 `_restore_latest()` 只在 `__init__` 里跑过一次，那时路径还是
        # 空的 → 直接 return → `_latest` 永远是 None。
        # 后果：页面一直显示“等待后台首轮决策”，而磁盘上明明已有
        # 2436 场结果（因为下一轮全量决策实测要 50+ 分钟）。
        #
        # 这与 `ledger_root` 是**同一类初始化时序缺陷**，一并在此修复。
        old_rp = getattr(old, "result_path", None) if old else None
        if old is None or old_rp != value.result_path:
            self._restore_latest()

    # -- 后台定时决策（主路径） -------------------------------------------

    def start_cycle(self) -> bool:
        """启动后台定时决策循环（幂等）。返回是否实际启动。"""
        if self._cycle_thread is not None and self._cycle_thread.is_alive():
            return False
        if self.config.cycle_interval_s <= 0:
            return False
        self.betting.start()
        self._cycle_stop.clear()
        self._cycle_thread = threading.Thread(
            target=self._cycle_loop, name="analysis-cycle", daemon=True)
        self._cycle_thread.start()
        return True

    def stop_cycle(self, timeout: float = 5.0) -> None:
        self.betting.stop()
        self._cycle_stop.set()
        t = self._cycle_thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        self._flush_decision_history()

    @property
    def cycle_running(self) -> bool:
        return self._cycle_thread is not None and self._cycle_thread.is_alive()

    def _cycle_loop(self) -> None:
        """定时跑决策。首轮立即执行（不等一个间隔），让系统尽快有数据。

        当变动触发式调度器在跑时，本循环只做**兜底**：
        频率降为 `max(cycle_interval_s, 10×change_debounce_s)`，
        避免“触发式刚算完、定时又全量重算一遍”的双重浪费。
        """
        interval = max(10.0, _to_float(self.config.cycle_interval_s, 600.0))
        if self.config.change_trigger:
            # 触发式为主时，定时只负责“长时间无行情变动”的兜底
            floor = 10.0 * max(1.0, _to_float(
                self.config.change_debounce_s, DEFAULT_CHANGE_DEBOUNCE_S))
            interval = max(interval, floor)
        while not self._cycle_stop.is_set():
            self._run_cycle_once()
            if self._fast_live_enabled():
                interval = min(interval, 60.0)
            self.cycle_stats["next_at"] = time.time() + interval
            if self._cycle_stop.wait(interval):
                return

    # -- 盘口变动触发决策（主路径，用户要求的触发时机） ---------------------

    def notify_price_change(self, mids: Sequence[str]) -> None:
        """`RealtimeHub` 的盘口变动回调入口（**必须在毫秒级返回**）。

        本方法只做「登记 + 唤醒」，真正的 LLM 决策在调度线程里做。
        原因：回调跑在推送消费线程上，阻塞它会拖慢整个行情消费。

        防抖语义：同一场在 `change_debounce_s` 窗口内的多次变动合并为一次；
        后续变动合并但保持首个到期时间，持续跳价也能按时计算。

        Args:
            mids: 本批发生真实变动的赛事 ID（Hub 已去重）。
        """
        rt = self.realtime
        if rt is not None and any(rt.is_finished(str(mid)) for mid in mids):
            self._settle_wake.set()
        if not self.config.change_trigger or not mids:
            return
        debounce = max(0.0, _to_float(self.config.change_debounce_s,
                                      DEFAULT_CHANGE_DEBOUNCE_S))
        if self._fast_live_enabled():
            debounce = min(debounce, 0.15)
        due = time.time() + debounce
        queue_max = max(1, _to_int(self.config.change_queue_max,
                                   DEFAULT_CHANGE_QUEUE_MAX))
        with self._sched_lock:
            self._sched_stats["signals"] += len(mids)
            for mid in mids:
                if not mid:
                    continue
                if mid in self._pending:
                    self._sched_stats["coalesced"] += 1
                self._pending.setdefault(str(mid), due)
                self._pending_first.setdefault(str(mid), time.monotonic())
            # 队列保护：只保留最可能仍有价值的（最近变动的）赛事
            while len(self._pending) > queue_max:
                oldest = min(self._pending, key=lambda k: self._pending[k])
                self._pending.pop(oldest, None)
                self._pending_first.pop(oldest, None)
                self._sched_stats["dropped"] += 1
        self._sched_wake.set()

    def start_scheduler(self) -> bool:
        """启动变动触发式决策线程（幂等）。返回是否实际启动。"""
        if not self.config.change_trigger:
            return False
        if self._sched_thread is not None and self._sched_thread.is_alive():
            return False
        self.live_expert.start(self.config.ledger_root)
        self.live_review.start()
        self.betting.start()
        self._sched_stop.clear()
        self._sched_thread = threading.Thread(
            target=self._sched_loop, name="analysis-trigger", daemon=True)
        self._sched_thread.start()
        return True

    def stop_scheduler(self, timeout: float = 5.0) -> None:
        self._sched_stop.set()
        self._sched_wake.set()
        t = self._sched_thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        self.betting.stop()
        self.live_review.stop()
        self.live_expert.stop()
        self._flush_decision_history()

    @property
    def scheduler_running(self) -> bool:
        return (self._sched_thread is not None
                and self._sched_thread.is_alive())

    def _sched_loop(self) -> None:
        """后台循环：等「有赛事到期」→ 限流 → 批量决策。

        用 `Event.wait(timeout)` 而非 `sleep`，所以新信号能立即唤醒；
        同时 `min_interval` 保证不会比原来的定时轮询更激进。
        """
        while not self._sched_stop.is_set():
            wait_s = self._next_trigger_wait()
            if wait_s is None:
                # 无待办：睡着等新信号（不轮询，不烧 CPU）
                self._sched_wake.wait(1.0)
                self._sched_wake.clear()
                continue
            if wait_s > 0:
                self._sched_wake.wait(min(wait_s, 1.0))
                self._sched_wake.clear()
                continue
            self._sched_wake.clear()
            self._trigger_batch()

    def _next_trigger_wait(self) -> Optional[float]:
        """距下一批触发还有多少秒；`None` 表示无待办。"""
        min_gap = max(0.0, _to_float(self.config.change_min_interval_s,
                                     DEFAULT_CHANGE_MIN_INTERVAL_S))
        if self._fast_live_enabled():
            min_gap = min(min_gap, 0.05)
        now = time.time()
        with self._sched_lock:
            if not self._pending:
                return None
            last = _to_float(self._sched_stats.get("last_trigger_at"), 0.0)
            throttle = (last + min_gap) - now
            earliest = min(self._pending.values()) - now
        return max(earliest, throttle)

    def _effective_batch(self) -> int:
        """本批该取多少场（自适应限流，HANDOVER §3.4）。

        why：固定批量在 LLM 慢时会堆积（实测 pending 达 72），在 LLM 快时
        又白等。这里用**上一批的实测单场耗时**反推本批批量：

            batch = clamp(target_batch_s / per_match_s, batch_min, batch_max)

        首轮无样本，用配置的 `change_batch` 作起点。样本用指数滑动平均
        （α=0.5）平滑，避免单次抖动把批量推飞。

        Returns:
            本批最多处理的场次数（>= 1）。
        """
        if self._fast_live_enabled():
            return 64
        base = max(1, _to_int(self.config.change_batch, DEFAULT_CHANGE_BATCH))
        if not self.config.adaptive_throttle:
            return base
        with self._sched_lock:
            per = _to_float(self._sched_stats.get("per_match_s"), 0.0)
        if per <= 0.0:
            return base  # 首轮：无样本，先用配置值
        target = _to_float(self.config.batch_target_s, DEFAULT_BATCH_TARGET_S)
        lo = max(1, _to_int(self.config.batch_min, DEFAULT_BATCH_MIN))
        hi = max(lo, _to_int(self.config.batch_max, DEFAULT_BATCH_MAX))
        want = int(target / per) if per > 0 else base
        return max(lo, min(hi, want))

    def _observe_batch(self, n: int, elapsed_s: float) -> None:
        """记录一批的实测吞吐，供下一轮自适应。

        Args:
            n: 本批实际处理的场次数。
            elapsed_s: 本批墙钟耗时（秒）。
        """
        if n <= 0 or elapsed_s <= 0:
            return
        per = elapsed_s / float(n)
        with self._sched_lock:
            old = _to_float(self._sched_stats.get("per_match_s"), 0.0)
            # 指数滑动平均：α=0.5，新样本权重大，但单次离群不会支配
            self._sched_stats["per_match_s"] = (per if old <= 0.0
                                                else 0.5 * old + 0.5 * per)
            self._sched_stats["last_batch_n"] = n
            self._sched_stats["last_batch_s"] = round(elapsed_s, 2)

    def _trigger_batch(self) -> None:
        """取一批到期的赛事做决策（异常只记录，不退出循环）。"""
        now = time.time()
        batch_max = self._effective_batch()
        with self._sched_lock:
            due = [m for m, t in self._pending.items() if t <= now]
            if not due:
                return
            # 最久等待的优先（它们变动最早，行情已稳定）
            due.sort(key=lambda m: self._pending[m])
            picked = due[:batch_max]
            for m in picked:
                self._pending.pop(m, None)
            self._sched_stats["last_trigger_at"] = now
            self._sched_stats["last_trigger_mids"] = list(picked)
        t0 = time.time()
        try:
            res = self.decide_matches(picked)
            self._observe_batch(len(picked), time.time() - t0)
            with self._sched_lock:
                self._sched_stats["batches"] += 1
                self._sched_stats["triggered"] += len(picked)
            # 把触发式结果也落盘，重启后能看到最新一批
            if not self._fast_live_enabled():
                self._persist_latest(res)
        except Exception as exc:  # noqa: BLE001 - 单批失败不得杀死调度器
            self.cycle_stats["last_error"] = "触发决策失败: %s: %s" % (
                type(exc).__name__, exc)

    def _fast_live_enabled(self) -> bool:
        return bool(self.config.fast_live and self.realtime is not None
                    and callable(getattr(self.realtime, "decision_snapshot", None)))

    def _decide_live_matches(self, mids: Sequence[str]) -> Dict[str, Any]:
        rt = self.realtime
        if rt is None:
            return self.live_expert.results()
        for mid in mids:
            with self._sched_lock:
                first = self._pending_first.pop(mid, None)
            snapshot = rt.decision_snapshot(mid)
            try:
                row = self.live_expert.compute(snapshot, rt, first)
                if not row:
                    self.notify_price_change([mid])
                else:
                    self.betting.enqueue(row)
                    self.live_review.submit(row)
            except (ValueError, TypeError, ArithmeticError) as exc:
                self.live_expert.errors += 1
                self.cycle_stats["last_error"] = "实时模型: %s" % exc
        result = self.live_expert.results()
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._latest = result
        return result

    def decide_matches(self, mids: Sequence[str]) -> Dict[str, Any]:
        """只对指定赛事跑一轮决策，并**合并进** `_latest`。

        与 `decide_list` 的区别：不重新选候选、不重算全部赛事，
        只算 `mids` 里的场次，然后把结果**合并**到最近一轮结果中。

        为何要合并而不是覆盖：行情是逐场跳动的，若每批都覆盖，
        页面上其它场次会瞬间变成“无数据”。合并能保证列表始终完整。
        """
        if self._fast_live_enabled():
            return self._decide_live_matches(mids)
        want = {str(m) for m in mids if m}
        if not want:
            return self._latest or {"count": 0, "summary": {}, "decisions": []}
        # 先把这几场的**最新赔率**拉回来，否则算的是旧价（见 refresh_matches）
        refresh = self.refresh_matches(sorted(want))
        # 用候选表把 mid 映射回完整赛事信息（联赛/队名/快照）
        cand_by_id = {str(m.get("match_id")): m
                      for m in self.candidates(only_live=False)}
        items: List[Tuple[str, str, str, str, List[Any], Any, Dict[str, Any]]] = []
        # 同样批量取（触发式决策每批最多 12 场，但批量版统一路径更简单，
        # 且避免了 per-match 的指纹校验）。
        snaps_by_mid = self._snapshots_for_many(sorted(want))
        for mid in want:
            m = cand_by_id.get(mid)
            if m is None:
                continue  # 已结束/不在候选里：静默跳过
            snaps = snaps_by_mid.get(mid)
            if not snaps:
                continue
            trend = self.realtime.trend(mid) if self.realtime else None
            items.append((mid, str(m.get("league") or ""),
                          str(m.get("home") or ""), str(m.get("away") or ""),
                          snaps, trend, self._build_context(m)))
        if not items:
            return self._latest or {"count": 0, "summary": {}, "decisions": []}

        workers = (max(1, _to_int(self.config.llm_concurrency, 4))
                   if self.match_engine.llm_available else 1)
        fresh: List[MatchPicks] = []
        t0 = time.time()
        if len(items) == 1 or workers <= 1:
            for it in items:
                try:
                    fresh.append(self.match_engine.decide_match(
                        it[4], trend=it[5], context=it[6]))
                except Exception as exc:  # noqa: BLE001
                    fresh.append(self._failed_pick(it[0], exc))
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(self.match_engine.decide_match,
                                    it[4], trend=it[5], context=it[6]): it
                        for it in items}
                for fut in as_completed(futs):
                    it = futs[fut]
                    try:
                        fresh.append(fut.result())
                    except Exception as exc:  # noqa: BLE001
                        fresh.append(self._failed_pick(it[0], exc))
        return self._merge_into_latest(fresh, t0, refresh=refresh)

    def _merge_into_latest(self, fresh: Sequence[MatchPicks],
                           t0: float,
                           refresh: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """把新算的几场合并进 `_latest`，保持整表完整。"""
        with self._lock:
            prev = self._latest or {}
            by_id: Dict[str, Dict[str, Any]] = {}
            for row in prev.get("decisions") or []:
                if isinstance(row, Mapping):
                    by_id[str(row.get("match_id"))] = dict(row)
            for r in fresh:
                by_id[r.match_id] = r.as_dict()
            rows = sorted(by_id.values(),
                          key=lambda r: _to_float(r.get("rank_score")), reverse=True)
            summary = {
                "n": len(rows),
                "buy": sum(1 for r in rows if r.get("has_buy")),
                "no_llm": sum(1 for r in rows
                              if r.get("decision") == DECISION_NO_LLM),
                "avoid": sum(1 for r in rows
                             if r.get("decision") == DECISION_AVOID),
                "n_picks": sum(len(r.get("picks") or []) for r in rows),
            }
            res = {
                "count": len(rows),
                "summary": summary,
                "llm": self.match_engine.llm_health(),
                "elapsed_s": round(time.time() - t0, 2),
                "errors": [],
                "decisions": rows,
                "triggered": [r.match_id for r in fresh],
                "trigger": "price_change",
                "refresh": dict(refresh) if refresh else None,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
            self._latest = res
        # 落台账（锁外）：磁盘 IO 不应持锁
        self._record_ledger(fresh, "price_change")
        return res

    def _record_ledger(self, results: Sequence[MatchPicks],
                       trigger: str) -> None:
        """把决策写进台账（失败只记录，不影响决策返回）。"""
        self._archive_decision_run({'trigger': trigger, 'decisions': [r.as_dict() for r in results]})
        if not self.ledger.enabled:
            return
        try:
            for r in results:
                self.ledger.record_match(r, trigger=trigger)
        except Exception as exc:  # noqa: BLE001 - 留痕失败不得中断决策
            self.ledger.last_error = "record_match: %s: %s" % (
                type(exc).__name__, exc)

    # -- 赛后结算（本地统计 LLM 准确度） -----------------------------------

    def start_settler(self) -> bool:
        """启动后台结算线程（幂等）。返回是否实际启动。"""
        if not self.ledger.enabled:
            return False
        if _to_float(self.config.settle_interval_s, 0.0) <= 0:
            return False
        if self._settle_thread is not None and self._settle_thread.is_alive():
            return False
        self._settle_stop.clear()
        self._settle_thread = threading.Thread(
            target=self._settle_loop, name="analysis-settle", daemon=True)
        self._settle_thread.start()
        return True

    def stop_settler(self, timeout: float = 5.0) -> None:
        self._settle_stop.set()
        self._settle_wake.set()
        t = self._settle_thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)

    @property
    def settler_running(self) -> bool:
        """结算线程是否在跑（与 `cycle_running`/`scheduler_running` 对称）。"""
        return (self._settle_thread is not None
                and self._settle_thread.is_alive())

    def _settle_loop(self) -> None:
        interval = max(10.0, _to_float(self.config.settle_interval_s, 60.0))
        while not self._settle_stop.is_set():
            try:
                self.settle_finished()
            except Exception as exc:  # noqa: BLE001 - 单轮失败不得退出循环
                self.settle_stats["last_error"] = "%s: %s" % (
                    type(exc).__name__, exc)
            self._settle_wake.wait(interval)
            self._settle_wake.clear()

    def settle_finished(self) -> Dict[str, Any]:
        # Manual requests cannot run the same upstream batch concurrently.
        if not self._settle_run_lock.acquire(blocking=False):
            return {'pending':True, 'reason':'结算正在进行', 'settle':dict(self.settle_stats)}
        try:
            return self._settle_finished_once()
        finally:
            self._settle_run_lock.release()

    def _settle_finished_once(self) -> Dict[str, Any]:
        """拉取已结束赛事的比分，回填台账并算出本地统计。

        ## 三条比分来源（按优先级）

        1. **`odds()`（唯一可靠来源）** —— 走
           `structureMatchBaseInfoByMidsPB`，带 `msc` 比分。
        2. **`schedule()`** —— 只用于筛「哪些场已结束」；
           实测它**不返回比分字段**（详见 `_finished_matches`）。
        3. **本地推送**（`RealtimeHub`）—— REST 不可用时的兑底。

        为何需要第 1 条（用户报「买入决策正确率没有统计」的根因）：
        早期只用 `schedule()`，而它根本不带 `msc` → `score` 恒为 `None`
        → `graded=0` → **命中率/ROI/CLV 永远算不出来**。

        Returns:
            结算统计（含 `stats`，即当前命中率/ROI/CLV）。
        """
        self.settle_stats["last_at"] = time.time()
        pending = set(self.ledger.pending_match_ids())
        if not pending:
            self.settle_stats["rounds"] = \
                _to_int(self.settle_stats.get("rounds")) + 1
            # 字段集与正常路径保持一致（前端/调用方不必区分两种返回形态）
            return {"settled": 0, "void": 0, "reason": "无待结算条目",
                    "closing_captured": 0, "scores_from_push": 0,
                    "scores_total": 0, "stats": self.ledger.stats()}
        # **先确定“哪些场真的能结算”**，再只对这些场做昂贵操作。
        #
        # 为何要调顺序（实测真实事故）：`_capture_closing_odds` 会读全库
        # 快照（仓库已累积 8.4 万条，冷缓存重建一次 **19~24s**）。
        # 早期它对**全部** pending（实测 535 场）都做一遍，
        # 而其中绝大多数早已结束且上游不再提供赛果 ——
        # 永远不可能被结算 → 那些读盘是**纯浪费**，
        # 直接把结算接口拖到 150s 超时。
        #
        # 新顺序：先用廉价手段（赛程/本地推送）算出能结算的场次，
        # 只对它们补收盘价 —— 既省时间，又完全契合 CLV 的语义
        # （收盘价本就是“该场即将结算时最后一次看到的价”）。
        scores: Dict[str, Any] = {}
        # Settle persisted terminal push evidence before any upstream work.
        local_used = self._merge_local_scores(scores, pending, confirmed=set())
        local_out = self.ledger.settle(scores) if scores else {'settled':0,'void':0,'scanned':0,'skipped':0}
        self.live_expert.update_evidence(self.ledger.performance_evidence())
        ordered = sorted(pending - set(scores))
        cursor = getattr(self, '_settle_cursor', 0) % max(1,len(ordered))
        batch = set((ordered+ordered)[cursor:cursor+min(60,len(ordered))])
        self._settle_cursor = cursor + len(batch)
        if time.monotonic()-getattr(self,'_settle_remote_at',-math.inf)<60:
            self.settle_stats['rounds'] = _to_int(self.settle_stats.get('rounds'))+1
            for key in ('settled','void'):
                self.settle_stats[key] = _to_int(self.settle_stats.get(key))+local_out[key]
            self.settle_stats.update(pending_matches=len(self.ledger.pending_match_ids()), scores_found=len(scores))
            return {**local_out,'scores_total':len(scores),'scores_from_push':local_used,
                    'closing_captured':0,'stats':self.ledger.stats()}
        self._settle_remote_at = time.monotonic()
        confirmed = self._confirmed_finished_mids(batch)
        # 本地推送兑底（廉价：纯内存）—— 先做，以便确定可结算集合
        local_used += self._merge_local_scores(scores, batch, confirmed=confirmed)
        # REST 补比分：只对**待结算**的场次调上游
        for mt in self._finished_matches(mids=batch):
            mid = str(getattr(mt, "mid", "") or "")
            if mid not in pending:
                continue
            ft = getattr(mt, "score", None)
            ht = getattr(mt, "half_score", None)
            if not ft or ft[0] is None or ft[1] is None:
                continue
            entry: Dict[str, Any] = {"ft": list(ft)}
            # 半场比分缺失时不填：上半场盘口会被判 void，而不是拿全场比分硬算
            if ht and ht[0] is not None and ht[1] is not None:
                entry["ht"] = list(ht)
            scores[mid] = entry
        # **只对确实能结算的场次**补收盘赔率（CLV 的前置条件，不能事后重建）。
        # 必须在 settle 之前做：settle 会把条目改成终结态，
        # 之后 capture_closing 就不再碰它（避免把状态改回 pending）。
        pregame = self.ledger.pregame_pending_ids()
        closing = self._capture_closing_odds(set(scores) & pregame) if set(scores) & pregame else 0
        # 显式注解：`settle()` 返回 Dict[str, int]，但下面要挂 `stats`（嵌套字典），
        # 不收宽类型会让静态检查拒绝赋值。
        out: Dict[str, Any] = dict(self.ledger.settle(scores))
        for key in ('settled','void','scanned','skipped'):
            out[key] += local_out[key]
        self.live_expert.update_evidence(self.ledger.performance_evidence())
        self.settle_stats.update(pending_matches=len(self.ledger.pending_match_ids()), scores_found=len(scores),
                                 source_batch=len(batch), evidence_missing=len(pending-set(scores)))
        out["closing_captured"] = closing
        out["scores_from_push"] = local_used
        out["scores_total"] = len(scores)
        self.settle_stats["rounds"] = \
            _to_int(self.settle_stats.get("rounds")) + 1
        self.settle_stats["settled"] = \
            _to_int(self.settle_stats.get("settled")) + _to_int(out.get("settled"))
        self.settle_stats["void"] = \
            _to_int(self.settle_stats.get("void")) + _to_int(out.get("void"))
        out["stats"] = self.ledger.stats()
        return out

    def _capture_closing_odds(self, pending: set) -> int:
        """为待结算场次补齐**收盘赔率**（CLV 必需），返回写入条数。

        为何用快照库而不是实时推送：快照库保留全部历史，
        而推送只覆盖“当前已订阅”的场次且重启即丢。
        取每场每个盘口的最新快照赔率，即最接近收盘的可得价格。

        ⚠️ 诚实边界：这**不是**严格意义的官方收盘价，而是
        “本系统最后一次看到的赔率”。对于已停止刷新的已结束赛事，
        两者通常一致；若上游早已停推而快照陈旧，CLV 会偏。
        因此 CLV 应按“同口径”解读，不能当成交易台精确核算。
        """
        want = {str(m) for m in pending if m}
        if not want:
            return 0
        quotes: Dict[Any, float] = {}
        # **一次批量取快照**，而不是逐场调 `_snapshots_for()`。
        #
        # 为何（实测性能故障）：轮询中的 pending 可达几十场（本仓库 34 场），
        # 而每次 `_snapshots_for()` 都会经 `_all_snapshots()` 校验存储指纹
        # （rglob 3111 个目录）—— 34 次就是 34 轮全盘扫描，且与决策/推送
        # 线程争抢缓存锁。py-spy 直接拍到 `analysis-settle` 线程卡在
        # `_store_stamp → rglob`，容器 CPU 被抬到 100%+、结算接口超时。
        #
        # 批量版只校验一次，并复用 `_all_snapshots()` 已有的
        # `match_id -> 下标` 索引。
        by_mid = self._snapshots_for_many(want)
        for mid, snaps in by_mid.items():
            # 快照按 captured_at 升序；后用覆盖前用 → 最新在手
            for snap in snaps:
                line = str((snap.metadata or {}).get("leyu_hv") or "")
                outcomes = tuple(snap.outcomes or ())
                odds = tuple(snap.odds or ())
                for i, oc in enumerate(outcomes):
                    if i >= len(odds):
                        break
                    try:
                        val = float(odds[i])
                    except (TypeError, ValueError):
                        continue
                    if val > 1.0:
                        quotes[(mid, str(snap.market), line, str(oc))] = val
        try:
            return self.ledger.capture_closing(quotes)
        except Exception as exc:  # noqa: BLE001 - 留痕失败不应阻断结算
            self.settle_stats["last_error"] = "收盘赔率写入失败: %s" % exc
            return 0

    def _snapshots_for_many(self, mids: Any) -> Dict[str, List[Any]]:
        """批量版 `_snapshots_for`：**只校验一次存储指纹**。

        为何需要：`_snapshots_for()` 每调一次都会经 `_all_snapshots()`
        校验存储指纹（rglob 3111 个目录）。逐场调用（结算几十场、
        决策上百场）会把同一份校验重复几十次 —— 实测能把 CPU 抬到
        100%+ 并使接口超时。本方法复用 `_all_snapshots()` 的
        `match_id → 下标` 索引，一次拿到所有需要的场次。

        Args:
            mids: 需要的赛事 ID 集合/序列。

        Returns:
            `{mid: [OddsSnapshot, ...]}`（按 captured_at 升序）。
        """
        need = {str(m) for m in mids if m}
        out: Dict[str, List[Any]] = {}
        if not need:
            return out

        # 1) 优先实时表（毫秒级 + 必然最新）
        rt = self.realtime
        if rt is not None:
            src = getattr(self.valuation, "source", None)
            name = getattr(src, "display_source", "") or ""
            for mid in need:
                try:
                    rows = rt.live.book(mid)
                except AttributeError:
                    break            # 旧版 Hub 无 LiveBook → 整体走磁盘
                if not rows:
                    continue
                home, away, league = self._match_names(mid)
                live = snapshots_from_live(
                    rows, mid, home=home, away=away, league=league,
                    source=name)
                if live:
                    out[mid] = live

        # 2) 剩余场次：逐场走 `_snapshots_of`（**单一事实源**，
        #    便于测试替身与未来改动只在那一处生效）。
        #
        #    ⚠️ 性能关键（实测真实事故）：`_snapshots_of` 底层是
        #    `_all_snapshots()` —— 全量读盘。虽然它带缓存，
        #    但缓存指纹 TTL 过期后**一次重建要 19s**（仓库已累积 8.4 万条快照）。
        #    结算逐场调用时会反复触发重建 → 整个结算接口超时（实测 >150s）。
        #
        #    因此先**一次性预热缓存**（失败不影响流程），
        #    后续每次 `_snapshots_of` 都命中缓存（实测 0.0001s/次）。
        rest = need - set(out)
        if rest:
            try:
                self.valuation._all_snapshots()   # 预热：只跑一次
            except Exception:  # noqa: BLE001 - 预热失败则逐场自行降级
                pass
            for mid in rest:
                try:
                    snaps = self.valuation._snapshots_of(mid)
                except Exception:  # noqa: BLE001 - 单场失败不影响其他场
                    continue
                if snaps:
                    out[mid] = snaps
        return out

    def _finished_matches(
        self, mids: Optional[Any] = None,
    ) -> List[Any]:
        """已结束**且能拿到终场比分**的赛事。取不到时返回空列表。

        ## 为何必须额外调 `odds()`（本项目真实缺陷，用户问题 2 的根因）

        用户报「本地买入决策的正确率没有做统计」。排查结论：

        * `source.schedule()`（走 `getOriginalDataPB`）**不返回比分字段**
          —— 实测全部已结束赛事的 `score_raw` 均为空串，
          所以 `_finished_matches()` 拿到的 `score` 恒为 `(None, None)`，
          `settle_finished()` 因此永远集不到赛果 → `graded=0`
          → 命中率/ROI/CLV **永远算不出来**。
        * 而 `source.odds(mids)`（走 `structureMatchBaseInfoByMidsPB`）
          **带 `msc`**（实测 `S2|0:1,S1|1:2,…`），是全仓唯一可靠的赛果来源。

        因此这里改为：先用赛程筛出「已结束」的场次，再用 `odds()` 批量
        拉回它们的比分。只对**台账里真的待结算**的场次调 `odds()`
        （由调用方传入 `mids`），避免为几千场无关赛事打上游。

        Args:
            mids: 需要结算的赛事 ID；为空时不过滤（兼容旧调用）。

        Returns:
            带终场比分的 `LEYUMatch` 列表。
        """
        try:
            sched = self.valuation.source.schedule()
        except Exception:  # noqa: BLE001 - direct historical lookup can still work
            sched = []
        by_schedule = {str(m.mid): m for m in (sched or [])}
        want = {str(m) for m in mids} if mids else set(by_schedule)
        ready = {mid: m for mid, m in by_schedule.items() if mid in want and m.is_finished}
        # Query absent IDs directly: the current live schedule omits old matches.
        missing = want - set(by_schedule)
        fetch = missing | {mid for mid, m in ready.items() if getattr(m, 'score', (None, None))[0] is None}
        if fetch:
            try:
                detailed = self.valuation.source.odds(sorted(fetch))
            except Exception:  # noqa: BLE001
                detailed = []
            for m in detailed:
                mid = str(m.mid)
                if mid in fetch and getattr(m, 'is_finished', False) is True:
                    ready[mid] = m
                elif mid in ready:
                    ready.pop(mid, None)  # contradictory new status is not a final-score proof
        return [m for m in ready.values() if getattr(m, 'score', (None, None))[0] is not None]

    def _confirmed_finished_mids(self, pending: Any) -> set:
        """赛程中 `ms==3`（已结束）且属于待结算集合的 mid。

        为何单独抽一个方法（而不用 `_finished_matches`）：
        后者会额外调 `odds()` 补比分，**on `odds()` 失败就返回空**，
        于是“已结束”这个信息也一并丢失了 —— 而它是结算的安全前提。
        本方法只看赛程，不依赖任何补数据步骤，因此稳得多。

        Returns:
            去重后的 mid 集合；取不到赛程时返回空集。
        """
        want = {str(m) for m in (pending or ()) if m}
        if not want:
            return set()
        try:
            sched = self.valuation.source.schedule()
        except Exception:  # noqa: BLE001 - 取不到赛程不影响其它功能
            return set()
        return {str(getattr(m, "mid", "")) for m in (sched or ())
                if getattr(m, "is_finished", False)
                and str(getattr(m, "mid", "")) in want}

    def _merge_local_scores(self, scores: Dict[str, Any],
                            pending: Any,
                            confirmed: Any = None) -> int:
        """把 **本地推送** 里的比分合并进 `scores`（REST 不可用时的兑底）。

        为何需要（用户报「买入决策正确率没有统计」的根因）：
        结算原只信任 REST 赛程，而它**不返回比分**；本地推送里的比分
        （`C103`）与结束通知（`C109`）才是可靠来源。

        ⚠️ **安全红线 —— 只结算“已确认结束”的场次**。
        进球过程中推送的是**当前比分**而非终场比分，拿它结算会把
        “还在踢”的比赛算成已定输赢 —— 那种统计比没有统计更危险。

        结束证据有两个来源（取并集），
        因为只认 `C109` 会漏掉大量真实已结束的场次（实测 scores.json 里
        17 场有比分却 `done=False`，永远不会被结算）：

          1. `C109` 推送（`rt.finished_mids()`）—— 实时、但只覆盖
             推送存活期间且订阅到的场次；
          2. **赛程 `ms==3`**（由调用方通过 `confirmed` 传入）——
             覆盖更全（重启后仍有）。

        Args:
            scores: 已有的 `{mid: {"ft": [...], "ht": [...]}}`（就地修改）。
            pending: 仍待结算的 mid 集合。
            confirmed: 已由**其它权威来源**（如赛程 `is_finished`）
                确认结束的 mid 集合。

        Returns:
            本次从推送补充的场次数。
        """
        rt = self.realtime
        if rt is None or not pending:
            return 0
        try:
            local = rt.scores_snapshot()
            finished = set(rt.finished_mids())
        except AttributeError:
            return 0            # 旧版 Hub 无这些能力
        # 合并两路结束证据（安全前提不变：必须确证结束）
        finished |= {str(m) for m in (confirmed or ())}
        want = {str(m) for m in pending if m}
        added = 0
        for mid, ft in local.items():
            mid = str(mid)
            if mid not in want or mid in scores:
                continue
            if mid not in finished:
                continue        # 未完赛不得结算（见上）
            try:
                h, a = int(ft[0]), int(ft[1])
            except (TypeError, ValueError, IndexError):
                continue
            entry: Dict[str, Any] = {"ft": [h, a]}
            # 半场比分缺失就不填：半场盘口会被判 void，
            # 而不是拿全场比分硬算（与 REST 分支同一约定）。
            # `int()` 必须在 try 内：脏值不得中断整轮结算。
            try:
                ht = rt.half_score(mid)
            except (AttributeError, TypeError):
                ht = None
            if ht and ht[0] is not None and ht[1] is not None:
                try:
                    entry["ht"] = [int(ht[0]), int(ht[1])]
                except (TypeError, ValueError):
                    pass        # 脏半场比分宁可不要（会被判 void）
            scores[mid] = entry
            added += 1
        return added

    def ledger_stats(self, only_picks: bool = True,
                     trigger: Optional[str] = None) -> Dict[str, Any]:
        """对外暴露的本地统计入口（API 直接用）。"""
        out = self.ledger.stats(only_picks=only_picks, trigger=trigger)
        out["ledger"] = self.ledger.health()
        out["settle"] = dict(self.settle_stats)
        return out

    def ledger_history(self, only_picks: bool = True, limit: int = 300,
                       days: int = 0, competition_type: Optional[str] = None,
                       algorithm: Optional[str] = None, cohort: str = "all", offset: int = 0) -> Dict[str, Any]:
        """历史决策 vs 实际结果的分组统计 + 明细（用户要求的菜单数据）。

        与 `ledger_stats` 的分工：后者是“一句话结论”（总命中率/ROI），
        前者是“逐条申诉材料”（分日期/联赛/盘口 + 每条实际比分），
        用于回答“为什么不行、哪一类不行”。

        同时补上结算线程的状态，让用户知道“未结算的还要等多久”。
        """
        out = self.ledger.history(picks_only=only_picks, limit=limit,
                                  days=days, competition_type=competition_type, algorithm=algorithm, cohort=cohort, offset=offset)
        out["settle"] = dict(self.settle_stats)
        out["summary"] = out["overall"]
        cfg = self.runtime_settings.config
        from core.ensemble import adaptive_weights
        evidence = self.ledger.performance_evidence()
        out["algorithm_weights"] = adaptive_weights(cfg.algorithms, evidence, cfg.weight_prior_matches, cfg.max_algorithm_weight)
        from core.ensemble import algorithm_alerts
        out["algorithm_alerts"] = algorithm_alerts(cfg.algorithms, evidence, cfg.algorithm_alert_min_samples,
                                                     cfg.algorithm_alert_threshold, cfg.algorithm_alert_enabled)
        return out

    def refresh_live_matches(self, max_matches: int = 0,
                             progress: Optional[Any] = None) -> Dict[str, Any]:
        """把**真实进行中**的赛事盘口拉取并落库。

        为何必须做：快照库是历史全量采集的产物，而**进行中赛事集合一直在变**。
        实测：乐鱼真实进行中足球 48 场，快照库里只有 9 场 ——
        其余 39 场根本没快照（它们当时还没开赛），因此决策只能覆盖 9 场。

        本方法每轮决策前调用：先拉这些赛事的盘口 → 归一化落库，
        决策就能覆盖全部进行中赛事。

        Args:
            max_matches: 单次刷新上限（防一次性打爆上游）。
            progress: 可选进度回调。

        Returns:
            统计字典（\u5305\u542b requested/stored/snapshots/issues）。
        """
        live_ids = self.live_match_ids()
        if not live_ids:
            return {"requested": 0, "stored": 0, "snapshots": 0,
                    "skipped": "无进行中赛事或赛程不可用"}
        mids = sorted(live_ids)
        if max_matches > 0:
            mids = mids[:max_matches]
        try:
            matches = self.valuation.source.odds(mids, progress=progress)
        except Exception as exc:  # noqa: BLE001 - 刷新失败不应中断决策（用旧快照也能算）
            return {"requested": len(mids), "stored": 0, "snapshots": 0,
                    "error": "%s: %s" % (type(exc).__name__, exc)}
        snaps: List[Any] = []
        issues: List[str] = []
        if self._fast_live_enabled():
            self.realtime.seed_matches(matches)  # type: ignore[union-attr]
        for mt in matches:
            snaps.extend(snapshots_from_match(
                mt, source=self.valuation.source.display_source, issues=issues))
        written = self.valuation.store.append_many(snaps)
        # 同理用增量合并（全量刷新时一次处理上百场，若整体失效
        # 会让紧随其后的决策重建 14s 级缓存）。
        try:
            self.valuation.merge_snapshots(snaps)
        except AttributeError:
            try:
                self.valuation.invalidate_cache()
            except AttributeError:
                pass
        return {"requested": len(mids), "returned": len(matches),
                "stored": len(written), "snapshots": len(snaps),
                "issues": len(issues)}

    def refresh_matches(self, mids: Sequence[str]) -> Dict[str, Any]:
        """只刷新指定赛事的盘口快照（盘口变动触发时用）。

        为何必需（否则触发式决策是错的）：`RealtimeHub` 的推送只写**走势**
        （`trend_store`），**不写**快照库（`valuation.store`）。快照库原本
        只在 `refresh_live_matches()` 里批量刷新。若触发时直接拿快照库
        去算 edge，用的还是**旧赔率** —— 决策与触发原因脱节，等于白算。

        与 `refresh_live_matches` 的区别：只拉这几场（几十场全拉在秒级
        触发下太重），且**不**失效全量缓存。
        """
        want = [str(m) for m in mids if m]
        if not want:
            return {"requested": 0, "stored": 0}
        try:
            matches = self.valuation.source.odds(want)
        except Exception as exc:  # noqa: BLE001 - 刷新失败则用旧快照继续（宁旧勿无）
            return {"requested": len(want), "stored": 0,
                    "error": "%s: %s" % (type(exc).__name__, exc)}
        snaps: List[Any] = []
        issues: List[str] = []
        if self._fast_live_enabled():
            self.realtime.seed_matches(matches)  # type: ignore[union-attr]
        for mt in matches:
            snaps.extend(snapshots_from_match(
                mt, source=self.valuation.source.display_source, issues=issues))
        written = self.valuation.store.append_many(snaps)
        # **增量合并**，而不是整体失效。
        #
        # ⚠️ 本项目真实性能故障：`invalidate_cache()` 会让下一次
        # `_all_snapshots()` 重扫全库 —— 实测 65,942 个文件需 **14~15s**。
        # 而本方法在“盘口变动触发”下会被**持续高频**调用，于是每批都重扫
        # 全库：实测容器 CPU 打满 99%、`/health` 被拖到 30s+ 超时，
        # 整个服务看起来像挂了。
        # 合并只把本批新快照并入缓存（O(本批条数)），代价可忽略。
        try:
            self.valuation.merge_snapshots(snaps)
        except AttributeError:
            # 兼容旧版 ValuationService（无该方法时退回整体失效）
            try:
                self.valuation.invalidate_cache()
            except AttributeError:
                pass
        return {"requested": len(want), "returned": len(matches),
                "stored": len(written), "snapshots": len(snaps)}

    def _run_cycle_once(self) -> None:
        """执行一轮决策并缓存结果。异常只记录，不让循环退出。"""
        self.cycle_stats["last_started"] = time.time()
        try:
            # 先刷新进行中赛事的盘口（否则新开赛的赛事无快照可算）
            if self.config.cycle_live_only:
                self.cycle_stats["refresh"] = self.refresh_live_matches()
            if self._fast_live_enabled():
                self.notify_price_change(self.realtime.live.live_mids(max_age_s=30))  # type: ignore[union-attr]
                self.cycle_stats["rounds"] += 1
                self.cycle_stats["last_error"] = ""
                return
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

    def _archive_decision_run(self, res: Mapping[str, Any]) -> None:
        path = self.config.result_path
        root = Path(path).parent if path else Path(self.config.ledger_root) if self.config.ledger_root else None
        if root is None:
            return
        cfg, version = self.runtime_settings.snapshot()
        params = asdict(cfg)
        params.pop('llm_api_key')
        with self._history_lock:
            history_path = root / 'decision-runs.jsonl.gz'
            if self._decision_history is None or self._decision_history.path != history_path:
                self._decision_history = HistoryJournal(history_path)
            self._decision_history_pending.append(copy.deepcopy({**res,
                'at': datetime.now(timezone.utc).isoformat(), 'schema_version': 2,
                'config_version': version, 'config': params}))
            self._flush_decision_history()

    def _flush_decision_history(self) -> None:
        with self._history_lock:
            if self._decision_history is None:
                return
            try:
                self._decision_history.append(self._decision_history_pending)
                self._decision_history_pending.clear()
            except (OSError, ValueError, TypeError):
                # Do not overwrite this diagnostic at the end of a successful cycle.
                pass  # HistoryJournal exposes the error; queued rows stay pending.

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

    def latest_age_s(self) -> Optional[float]:
        """最近一轮决策结果距今多少秒；无结果或时间戳非法时返回 None。

        用途（用户报“加载不出来比赛场次”的根因之一）：
        调用方需要区分「从未产出过结果」与「结果太旧」——
        早期把两者都当成空，于是旧数据被隐藏，页面只能显示空态，
        而磁盘上其实有几千场决策。有了本方法就能**如实标注时效**。
        """
        with self._lock:
            res = self._latest
        if not res:
            return None
        fin = res.get("finished_at")
        if not fin:
            return None
        try:
            return max(0.0, (datetime.now(timezone.utc)
                             - datetime.fromisoformat(fin)).total_seconds())
        except (TypeError, ValueError):
            return None

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
        job_id = "job-%d-%04x" % (_to_int(time.time()), random.getrandbits(16))
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

    def _snapshots_for(self, mid: str) -> List[Any]:
        """取该场用于决策的快照：**优先本地实时表**，回退磁盘历史。

        ## 为何必须优先实时表（用户报告问题 2/3）

        磁盘快照是**不可变历史**：一场比赛同一盘口会累积几十份不同时间的
        记录（实测 `5726509` 共 216 条，`OU(2.5)` 重复 15 次，赔率 1.94→3.32）。
        两个后果：

          1. **慢**：读全库实测 14s（冷启动叠加 45s）；
          2. **取到过期价**：该场比分已 1:3（4 球），系统仍拿
             4.6 小时前的 3.32 算出「全场进球数>2.5 买入」——
             那个盘口早已结算，是**凭空造出的注单**。

        推送（`C105` 周期性全量快照）里本来就有**当前赔率**，
        `LiveBook` 已在内存留了一份，翻译即得。因此：

          * 有实时行情 → 用它（毫秒级 + 必然是最新价）；
          * 无实时行情（离线/体彩/未订阅）→ 回退快照库（兼容旧行为）。

        Returns:
            `OddsSnapshot` 列表（实时路径下每个盘口只有一份）。
        """
        rt = self.realtime
        if rt is not None:
            try:
                rows = rt.live.book(mid)
            except AttributeError:
                rows = None      # 旧版 Hub 无 LiveBook
            if rows:
                src = getattr(self.valuation, "source", None)
                name = getattr(src, "display_source", "") or ""
                # ⚠️ 必须补队名/联赛：推送只带赔率，不带队名。
                # 若缺了，`decide_match` 从 `snaps[0]` 取的 home/away
                # 会是空串，买入建议的中文标签就会退化成「主队/客队」
                # （用户明确要求「xx队上半场-1」这种带队名的写法）。
                home, away, league = self._match_names(mid)
                live = snapshots_from_live(
                    rows, mid, home=home, away=away, league=league,
                    source=name)
                if live:
                    return live
        return self.valuation._snapshots_of(mid)

    def _match_names(self, mid: str) -> Tuple[str, str, str]:
        """从目录名索引取 `(home, away, league)`；未知则返回空串。

        用 `match_index()`（仅扫目录名，实测 0.09s 且带 TTL 缓存），
        而不是 `list_matches()`（要读 6.5 万个快照文件，14s）。

        性能：`match_index()` 返回的是**列表**，直接遍历查找是 O(n)；
        而本方法在一轮里会被逐场调用（最多几千场）→ O(n×m)（实测
        3111×2436 多次比较，白白烧 CPU）。因此这里先转成 `mid → row`
        的字典并缓存，查找降为 O(1)。
        """
        row = self._names_dict().get(str(mid))
        if row is None:
            return "", "", ""
        return (str(row.get("home") or ""),
                str(row.get("away") or ""),
                str(row.get("league") or ""))

    def _names_dict(self) -> Dict[str, Dict[str, Any]]:
        """`mid → 名称行` 的字典缓存（避免逐场线性扫描）。

        与 `match_index()` 的 TTL 缓存配合：`match_index()` 本身已缓存
        扫描结果，这里只是把它重排成可 O(1) 查找的形状；
        用 `id()` 判定“列表对象是否换过”即可知道要不要重建。
        """
        try:
            rows = self.valuation.match_index()
        except Exception:  # noqa: BLE001 - 队名缺失只影响展示，不阻断决策
            return {}
        cached = self._names_cache
        if cached is not None and cached[0] is rows:
            return cached[1]
        table = {str(r.get("match_id")): r for r in rows}
        self._names_cache = (rows, table)
        return table

    def _build_context(self, match: Mapping[str, Any]) -> Dict[str, Any]:
        """构造交给决策引擎/LLM 的上下文（**严防比分泄漏**）。

        ⚠️ 本项目真实故障：早期实现把 `realtime.score(mid)` 与比赛时钟
        一并放进上下文，而 `MatchDecisionEngine.build_prompt` 会把它
        写进提示词 —— 于是 LLM 直接“抄答案”，实测理由原文为
        「终场0:0，小球」「主队2:0取胜，平手盘主胜」，
        并给出 `p_llm=1.0` 而市场仅 0.35 的虚假 edge。

        后果有两层：
          1. 制造大量**假买入信号**（用户看到的“建议”不可信）；
          2. 污染决策台账，事后统计出的命中率严重虚高，
             等于把答案泄给了考生，永远无法评估 LLM 真实水平。

        因此这里**只**传交易语义的状态（进行中/未开赛/已结束），
        比分与分钟数一律不下发。需要排查时可在 /board 查看比分 ——
        那是给人看的，不是给模型看的。
        """
        ctx: Dict[str, Any] = {"status_text": match.get("state")}
        mid = str(match.get("match_id", ""))
        if self.realtime is None:
            return ctx
        if self.realtime.is_finished(mid):
            ctx["status_text"] = "已结束"
        if not self.config.leak_score_to_llm:
            # 显式开关：默认不传。保留开关是为了让对照实验可复现
            # （用于证明“泄露比分 → 虚假 edge”这一结论）。
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
        return ctx

    def _candidate_is_fresh(self, mid: str, m: Mapping[str, Any],
                            is_live: bool) -> bool:
        """该场是否仍是「活跃的比赛」（而不是已结束/旧快照）。

        为何需要（真实故障的根因）：赛程接口一失败（本项目实测为
        会话 `token已过期`），`live_match_ids()` 就返回 None，
        `is_live` 退化成快照的 `state`；而 state 只看**快照时效**——
        一个两天前入库、比赛早已结束的赛事，其赔率仍会被上游
        周期刷新，于是 state 始终是 active，被当作进行中送去分析，
        LLM 又能看到终场比分（实测：`5720069` 快照来自 10-02，
        却在 10-04 被分析并给出 “终场0:0…” 的买入建议）。

        判据（依次）：
          1. 没有实时 Hub（离线/无推送）→ 不拦截，维持旧行为；
          2. 收到过比分推送：距上次比分更新的时长 ≤ `stale_score_s`；
          3. 从未收到比分推送：见下。

        关于第 3 种情形的保守处理：刚开赛的场次可能还没推比分，
        不能一刀切拦掉；但「很早就被 Hub 看到、却始终没有比分」
        几乎必然是**非活跃**场次。因此用「首次见到距今」时长判定：

          * 从未见过（重启后还没轮到它推送）→ 放行，避免误伤；
          * 见过但不超过 `max_live_age_s` → 放行（可能是刚开始的场）；
          * 见过且已超过 `max_live_age_s` 仍无比分 → 拦下。
        """
        rt = self.realtime
        if rt is None:
            return True
        age = rt.score_age_s(mid)
        if age is not None:
            return age <= self.config.stale_score_s
        seen = rt.first_seen_age_s(mid)
        if seen is None:
            return True
        return seen <= self.config.max_live_age_s

    def live_match_ids(self) -> Optional[set]:
        """真实**进行中**的赛事 ID 集合。

        ## 两条数据源（按可靠性排序）

        1. **推送流（首选回退）**：`RealtimeHub` 收到行情的场次就是真的在滚球。
           这是最接近乐鱼 App 自身行为的判据 —— 用户报告「leyu 显示 67 场但
           系统只统计 34」时，正是因为早期只依赖第 2 条而它挂了。
        2. **REST 赛程（优先）**：`source.schedule()` 的 `ms == 1`，语义最明确。

        为何不能只用 `list_matches()` 的 `state`：那个 `state` 是**快照时效**
        派生的（active/stale/delisted），与比赛真实是否进行中无关。实测：
        乐鱼真实进行中足球 44 场，用快照 state 筛选只能命中 2 场。

        ## 为何必须加推送回退（本项目真实故障）

        会话过期时 `schedule()` 抛 `SessionError` → 早期实现直接返回 `None`
        → 调用方退化成按快照 `state` 过滤（只看时效，不看是否在踢）
        → 用户看到「34 场 vs leyu 67 场」且混入大量已结束场次。
        而与此同时**推送链路是好的**（实测 51 万次赔率更新、订阅正常滚动），
        它本来就能告诉我们“哪些场子现在真的在动”。

        Returns:
            进行中 mid 集合；**两条源都不可用时返回 None**
            （调用方应回退到旧行为）。
        """
        source = getattr(self.valuation, "source", None)
        # 缓存一段很短时间：候选选取在一轮里可能被调多次
        now = time.time()
        cached = getattr(self, "_live_cache", None)
        if cached is not None and now - cached[0] < 30.0:
            return cached[1]

        live: Optional[set] = None
        err = ""
        if source is not None and hasattr(source, "schedule"):
            try:
                schedule = source.schedule()
            except Exception as exc:  # noqa: BLE001 - 取不到就用推送回退
                err = "%s: %s" % (type(exc).__name__, exc)
            else:
                # 只取**足球**（本项目是足球估值系统）：乐鱼同一网关也返回
                # 篮球/网球/排球等，不过滤会带入大量无法映射的盘口。
                live = set()
                by_type: Dict[str, set] = {}
                for match in schedule:
                    if not (match.is_live and match.sport_id == SOCCER_SPORT_ID):
                        continue
                    mid = str(match.mid)
                    live.add(mid)
                    kind = competition_type({
                        "league": match.tournament,
                        "home": match.home,
                        "away": match.away,
                        "sport": match.sport,
                        "sport_id": match.sport_id,
                    })
                    by_type.setdefault(kind, set()).add(mid)
                self._live_type_cache = (now, by_type)

        if live is None:
            # 回退：用推送流见过的场次（它们现在真的在跳赔）
            self._live_type_cache = None
            live = self._live_ids_from_push()
            if live is not None:
                by_type = {}
                for mid in live:
                    snapshot = (getattr(self.realtime, "decision_snapshot", None)
                                if self.realtime is not None else None)
                    try:
                        info = (snapshot(mid).get("info", {})
                                if callable(snapshot) else {})
                    except (AttributeError, TypeError):
                        info = {}
                    by_type.setdefault(competition_type(info), set()).add(mid)
                self._live_type_cache = (now, by_type)
                self._live_source = "push"
                self._live_error = err
            else:
                self._live_source = "none"
                self._live_error = err or "无可用数据源"
        else:
            self._live_source = "schedule"
            self._live_error = ""

        self._live_cache = (now, live)
        return live

    def live_match_ids_by_type(self, kind: str = "all", *,
                               refresh: bool = True) -> Optional[set]:
        """Return current football IDs split by real/virtual/unknown type."""
        if refresh:
            self.live_match_ids()
        cached = self._live_type_cache
        if cached is None:
            return None
        if kind in ("", "all"):
            out: set = set()
            for mids in cached[1].values():
                out.update(mids)
            return out
        return set(cached[1].get(kind, set()))

    def live_coverage(self, kind: str = "all") -> Dict[str, Any]:
        """Read background coverage caches without REST, disk or book scans.

        The upstream football counter includes virtual matches and the venue's
        pre-kickoff grace window. It is only comparable with the all-type view.
        Preserve cache age so an outage cannot turn old counts into fresh ones.
        """
        ids = self.live_match_ids_by_type(kind, refresh=False)
        cached = self._live_cache
        upstream = self._upstream_count_cache
        return {
            "source_current": len(ids) if ids is not None else None,
            "source_status": self._live_source or "unknown",
            "source_age_s": (round(max(0.0, time.time() - cached[0]), 1)
                             if cached is not None else None),
            "source_upstream": (upstream[1].get("upstream")
                                if kind == "all" and upstream else None),
            "source_derived": (upstream[1].get("derived")
                               if kind == "all" and upstream else None),
        }

    def _live_ids_from_push(self) -> Optional[set]:
        """从推送流推导进行中赛事（`LiveBook` 留存的最新赔率表）。

        过滤规则（为避免把刚结束/已陈旧的场当活跃）：
          * 排除 Hub 已收到结束通知（`C109`）的场；
          * 排除**行情本身**已超过 `push_quote_max_age_s` 的场
            （这是防「重启后旧行情冒充进行中」的关键门禁）。

        ⚠️ 比分的时效门禁**不在本方法**：它由 `_candidate_is_fresh()` 在候选
        阶段管（本方法只回答「哪些场现在真的在跳赔」）。

        ## 为何必须有行情新鲜度门禁（本项目真实故障）

        `LiveBook` 启动时从 `_live/live.json` 回填，而旧实现把回填行标成
        「刚写入」，于是 `/api/v1/analysis` 在会话过期、推送一条没收到
        （`connected=0, messages=0`）的情况下仍然声称
        `count=527, source=push` —— 把一个**完全死的采集链路**报成
        「527 场真实进行中」。加门禁后，陈旧回填一律不再算活跃，
        调用方据此不再做「进行中」判定（HANDOVER §6.3 的既定原则：
        拿不到权威来源时标「未知」，不冒充）。

        Returns:
            mid 集合；**无法判定时返回 None**（包括：Hub 未启用、无
            `LiveBook`、表里一行行情都没有、或行情**全部已陈旧**）。

            为何“全部陈旧”也归为 `None`（而不是空集）：空集会被下游读作
            「已确认 0 场进行中」，而“没有任何新鲜行情”并不能证明
            “没有比赛在踢”（采集可能只是断了）。误报 0 会导致看板在
            默认勾选「只看进行中」时**变空白且不告警** —— 那正是
            `api/app.py` 明确要防的「看板空白」故障。
            返回 `None` 则复用既有的诚实机制：`live_known=False` →
            前端显示「未知」+ 原因（HANDOVER §6.3 的既定原则）。

            *注*：真正“确认 0 场”的权威来源是**赛程**（`ms==1`，走
            `source.schedule()`）——那条路返回的空集确实是结论。
            本方法只是会话不可用时的**回退**，它拿不出结论就应说不知道。
        """
        rt = self.realtime
        if rt is None:
            return None
        book = getattr(rt, "live", None)
        if book is None:
            return None
        max_age = _to_float(self.config.push_quote_max_age_s,
                            DEFAULT_PUSH_QUOTE_MAX_AGE_S)
        if not _live_mids_supports_max_age(book):
            # 旧版/测试替身不支持按年龄取活跃场：**不能**退回「不过滤」
            # （那正是本方法要修的缺陷），而是自行用真实行情年龄把关。
            mids = self._live_mids_filtered_by_quote_age(book, max_age)
        else:
            mids = list(book.live_mids(max_age_s=max_age))
        if not mids:
            # 无任何**新鲜**行情 → 无法判定谁在踢，返回 None（未知）。
            # 绝不能返回空集：空集会被读作「已确认 0 场进行中」，
            # 使看板变空白且不告警（见 docstring Returns 的说明）。
            return None
        out: set = set()
        for mid in mids:
            try:
                if rt.is_finished(mid):
                    continue
            except Exception:  # noqa: BLE001 - 单个场次判定失败不影响其余
                pass
            out.add(str(mid))
        return out

    @staticmethod
    def _live_mids_filtered_by_quote_age(book: Any, max_age_s: float) -> list[str]:
        """`live_mids` 不支持年龄参数时的等价兜底（按上游行情年龄过滤）。"""
        try:
            raw = list(book.live_mids())
        except Exception:  # noqa: BLE001 - 拿不到行情等于没有活跃场次
            return []
        out: list[str] = []
        for mid in raw:
            try:
                quotes = book.book(mid)
                ages = [_to_float(getattr(q, "quote_age_s", float("inf")),
                                  float("inf")) for q in (quotes or ())]
            except Exception:  # noqa: BLE001 - 单场异常不影响其余场次
                ages = []
            if ages and min(ages) <= max_age_s:
                out.append(str(mid))
        return out

    def candidates(
        self,
        limit: Optional[int] = None,
        only_live: bool = False,
        league: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """选取待分析的候选赛事。

        优先「真实进行中」（来自数据源赛程的 ms==1），其次未开赛；
        已结束的不分析（无决策价值）。

        ⚠️ 不能用快照的 `state` 判断是否进行中：那个字段是**快照时效**
        派生的，实测会漏掉 42/44 场真实进行中的赛事。
        """
        matches = self.valuation.list_matches(league=league)
        live_ids = self.live_match_ids()
        out: List[Dict[str, Any]] = []
        for m in matches:
            st = str(m.get("state") or "")
            if st == "delisted":
                continue
            mid = str(m.get("match_id") or "")
            # 真实进行中：优先用赛程数据；取不到时退回快照 state
            is_live = (mid in live_ids) if live_ids is not None \
                else (st == "active")
            if only_live and not is_live:
                continue
            # 活跃度过滤：赛程接口失败时 `live_ids` 为 None，`is_live` 会
            # 退化成快照 state（只看时效），导致**几天前已结束**的赛事被
            # 当作进行中送去分析（真实故障：5720069 快照来自 10-02，
            # 却在 10-04 被分析）。此处用推送活跃度再卡一道。
            if is_live and not self._candidate_is_fresh(mid, m, is_live):
                continue
            mkt = self._market_for(m)
            if mkt is None:
                continue
            m = dict(m)
            m["_market"] = mkt
            m["_is_live"] = is_live
            out.append(m)

        # 进行中优先（实时价值最高），其余按时间
        def sort_key(m: Mapping[str, Any]) -> Tuple[int, str, str]:
            return (0 if m.get("_is_live") else 1,
                    str(m.get("date") or ""), str(m.get("match_id")))
        out.sort(key=sort_key)
        # limit 语义：
        #   None  → 用配置的 cycle_limit（0 也不限制）
        #   0     → **不限制**（覆盖全部进行中）
        #   >0    → 截断到该数
        # 早期实现直接 out[:cap]，于是 cap=0 会返回**空列表**，
        # 而 0 本该表示“不限”——这是个很容易踩的语义陷阱。
        cap = limit if limit is not None else self.config.cycle_limit
        cap = _to_int(cap)
        if cap <= 0:
            return out
        return out[:cap]

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
        self._archive_decision_run({'trigger': 'manual', 'decisions': [decision.as_dict()]})
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
            #
            # **批量取快照**（而不是逐场 `_snapshots_for()`）：
            # 后者每调一次都会校验存储指纹（rglob 3111 个目录），
            # 一轮 2436 场就是 2436 轮全盘扫描 —— 实测把 CPU 抬到 100%+、
            # 决策接口直接超时。批量版只校验一次。
            wanted = [str(m.get("match_id")) for m in cands]
            snaps_by_mid = self._snapshots_for_many(wanted)
            items: List[Tuple[str, str, str, str, List[Any], Any, Dict[str, Any]]] = []
            for m in cands:
                mid = str(m.get("match_id"))
                snaps = snaps_by_mid.get(mid)
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
            # 落台账（锁外）：这是“LLM 到底准不准”的唯一证据来源
            self._record_ledger(results, "cycle")
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
                # 触发时机可观测：页面/运维能直接看到当前靠什么触发
                "change_trigger": self.config.change_trigger,
                "change_debounce_s": self.config.change_debounce_s,
                "change_min_interval_s": self.config.change_min_interval_s,
                "cycle_interval_s": self.config.cycle_interval_s,
                # 自适应限流：当前批量与依据可观测（HANDOVER §3.4）
                "adaptive_throttle": self.config.adaptive_throttle,
                "batch_target_s": self.config.batch_target_s,
                "effective_batch": self._effective_batch(),
            },
            "cycle": dict(self.cycle_stats),
            "scheduler": self.scheduler_health(),
            "betting": self.betting.health(),
            "decision_history": {**(self._decision_history.health() if self._decision_history else {}),
                                 'pending': len(self._decision_history_pending)},
            # 进行中覆盖的可观测性：用户要能自己查「为什么比 leyu 少」。
            #
            # `upstream` 是**乐鱼页面滚动球计数同源**的值
            # （`platformsSportCountPB` → `TY.1.balls.1.ct`），
            # 用户问题 1「要与乐鱼一致」就能直接对账：
            #   count = 本系统订阅/展示的场次数
            #   upstream = 乐鱼页面那个数字
            #   derived = 纯按 ms==1 推导（不含开赛前宽限）
            "live": {
                "source": self._live_source or "未取样",
                "count": (len(self._live_cache[1])
                          if self._live_cache and self._live_cache[1] is not None
                          else None),
                "age_s": (round(time.time() - self._live_cache[0], 1)
                          if self._live_cache else None),
                "error": self._live_error,
                **(self._live_count_info()),
            },
        }
        if self.realtime is not None:
            out["realtime"] = self.realtime.health()
        return out

    def _live_count_info(self) -> Dict[str, Any]:
        """乐鱼上游自报的「滚动球」计数（供与页面直接对账）。

        为何需要（用户问题 1：「比赛场次要与乐鱼中的今日足球进行中一致」）：
        仅凭本系统的 `count`，用户无法知道“是不是少了”——
        必须有一个**同源参照值**才能叫“一致”。
        本方法取的就是乐鱼页面计数徒标同一个端点
        （`platformsSportCountPB` → 体育 > 滚球 > 足球的 `ct`）。

        失败不影响其它字段（对账是增强能力），且带短 TTL 缓存以免
        每次 `/analysis` 都打上游。
        """
        now = time.time()
        cached = self._upstream_count_cache
        if cached and (now - cached[0]) < 60.0:
            return cached[1]
        out: Dict[str, Any] = {"upstream": None, "derived": None}
        try:
            src = getattr(self.valuation, "source", None)
            info = src.live_count(SOCCER_SPORT_ID) if src is not None else None
            if isinstance(info, Mapping):
                out["upstream"] = info.get("live")
                out["derived"] = info.get("derived")
                out["upstream_source"] = info.get("source")
        except Exception:  # noqa: BLE001 - 对账失败不得影响 /analysis
            pass
        self._upstream_count_cache = (now, out)
        return out

    def scheduler_health(self) -> Dict[str, Any]:
        """变动触发式调度器的可观测状态。"""
        with self._sched_lock:
            pending = dict(self._pending)
            stats = dict(self._sched_stats)
        now = time.time()
        return {
            "running": self.scheduler_running,
            "mode": "live" if self._fast_live_enabled() else "legacy",
            "performance": self.live_expert.health(),
            "pending": len(pending),
            # 待办里最近/最早何时会被处理（前端可显示“正在等行情稳定”）
            "next_due_in_s": (round(min(pending.values()) - now, 1)
                              if pending else None),
            "stats": stats,
        }

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
        fast_live=(e.get("ANALYSIS_FAST_LIVE", "1").strip().lower() not in ("0", "false", "no")),
        use_llm=use_llm,
        max_analyze=_to_int(_num("ANALYSIS_MAX", DEFAULT_MAX_ANALYZE),
                            DEFAULT_MAX_ANALYZE),
        cache_ttl_s=_num("ANALYSIS_CACHE_TTL", DEFAULT_CACHE_TTL_S),
        # 定时决策（默认 180s）；ANALYSIS_CYCLE=0 可关闭
        cycle_interval_s=_num("ANALYSIS_CYCLE", DEFAULT_CYCLE_INTERVAL_S),
        cycle_limit=_to_int(_num("ANALYSIS_CYCLE_LIMIT", DEFAULT_CYCLE_LIMIT),
                            DEFAULT_CYCLE_LIMIT),
        cycle_live_only=(e.get("ANALYSIS_CYCLE_LIVE_ONLY", "1") or "1").strip()
                        not in ("0", "false", "no"),
        # 盘口变动触发决策（用户要求的触发时机）；ANALYSIS_CHANGE_TRIGGER=0 可关
        change_trigger=(e.get("ANALYSIS_CHANGE_TRIGGER", "1") or "1").strip()
                       not in ("0", "false", "no"),
        # 是否允许把比分/比赛时钟下发给 LLM。
        # **默认关闭**（比分泄露会让 LLM 拄答案）；ANALYSIS_LEAK_SCORE=1
        # 仅用于可复现的对照实验。
        leak_score_to_llm=(e.get("ANALYSIS_LEAK_SCORE", "0") or "0").strip()
                          in ("1", "true", "yes"),
        stale_score_s=_num("ANALYSIS_STALE_SCORE_S", DEFAULT_STALE_SCORE_S),
        push_quote_max_age_s=_num("ANALYSIS_PUSH_QUOTE_MAX_AGE_S",
                                   DEFAULT_PUSH_QUOTE_MAX_AGE_S),
        max_live_age_s=_num("ANALYSIS_MAX_LIVE_AGE_S",
                            DEFAULT_MAX_LIVE_AGE_S),
        # LLM 超时：实测默认 90s 使 11/71 场因超时降级，150s 覆盖尾部
        llm_timeout_s=_num("ANALYSIS_LLM_TIMEOUT", DEFAULT_LLM_TIMEOUT_S),
        max_prob_deviation=_num("ANALYSIS_MAX_PROB_DEVIATION",
                                DEFAULT_MAX_PROB_DEVIATION),
        max_markets_per_prompt=_to_int(
            _num("ANALYSIS_MAX_MARKETS", MAX_MARKETS_PER_PROMPT),
            MAX_MARKETS_PER_PROMPT),
        change_debounce_s=_num("ANALYSIS_CHANGE_DEBOUNCE",
                               DEFAULT_CHANGE_DEBOUNCE_S),
        change_min_interval_s=_num("ANALYSIS_CHANGE_MIN_INTERVAL",
                                   DEFAULT_CHANGE_MIN_INTERVAL_S),
        change_batch=_to_int(_num("ANALYSIS_CHANGE_BATCH", DEFAULT_CHANGE_BATCH),
                             DEFAULT_CHANGE_BATCH),
        adaptive_throttle=(e.get("ANALYSIS_ADAPTIVE_THROTTLE", "1") or "1")
                          .strip() not in ("0", "false", "no"),
        batch_target_s=_num("ANALYSIS_BATCH_TARGET_S", DEFAULT_BATCH_TARGET_S),
        batch_min=_to_int(_num("ANALYSIS_BATCH_MIN", DEFAULT_BATCH_MIN),
                          DEFAULT_BATCH_MIN),
        batch_max=_to_int(_num("ANALYSIS_BATCH_MAX", DEFAULT_BATCH_MAX),
                          DEFAULT_BATCH_MAX),
        change_queue_max=_to_int(
            _num("ANALYSIS_CHANGE_QUEUE_MAX", DEFAULT_CHANGE_QUEUE_MAX),
            DEFAULT_CHANGE_QUEUE_MAX),
        result_path=(e.get("ANALYSIS_RESULT_PATH") or "").strip() or None,
        ledger_root=(e.get("ANALYSIS_LEDGER_ROOT") or "").strip() or None,
        settle_interval_s=_num("ANALYSIS_SETTLE_INTERVAL", 60.0),
    )
    return AnalysisService(valuation=valuation, realtime=realtime, config=cfg)
