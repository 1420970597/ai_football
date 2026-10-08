#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""决策台账（ledger）—— 记录「算法说了什么」与「实际发生了什么」。

## 为什么需要它

用户的核心质疑是「LLM 判定准不准」。此前系统只输出**当下建议**，
没有任何回看机制：没人能回答「上周那些买入建议，命中率多少？」

本模块提供三件事：

1. **留痕**：每次决策产出的每个建议（含门槛、三套概率、融合权重）
   追加落盘为 JSONL —— 一行一条，追加写、崩溃不丢已写内容。
2. **结算**：拿到终场比分后，用 `core.settlement` 把每条建议判成
   赢/输/走水/赢半/输半，并记录**收盘赔率**以算 CLV。
3. **统计**：本地算出命中率 / ROI / CLV —— 全部可在容器内复核，
   不依赖任何外部服务。

## 为什么记「未通过门控」的盘口

只记买入建议会有严重的**选择偏差**：无法判断门控是在帮忙还是在
误杀。因此台账同时记录被拦截的盘口及其原因码，这样才能回答
「门控拦掉的那些，实际赢了多少」—— 这是校准门控阈值的唯一依据。

## 为何 CLV 比单场输赢更可信

单场结果方差极大，几十注看不出模型好坏；CLV（买入价 vs 收盘价）
衡量的是「是否持续拿到好价格」，样本需求低得多。这也是本项目
研究结论里明确写下的判据。

## 落盘格式

`<root>/ledger.jsonl` —— 每行一个 JSON 对象。追加写，不做原地修改
（避免半写文件）；每次建议有独立 `decision_id`，修订只更新该建议；`at` 永不改变。旧格式
缺少身份的数据沿用旧键去重，明确标注为不完整历史，不能恢复被覆盖的建议。
"""

from __future__ import annotations

import json
import math
import uuid
import os
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, cast

from core.settlement import (
    GRADED_STATUSES,
    SETTLE_PENDING,
    SETTLE_VOID,
    TERMINAL_STATUSES,
    clv,
    pnl_for,
    settle_pick,
    summarise,
)
from core.market_labels import describe_market

__all__ = ["LedgerEntry", "DecisionLedger"]

#: 台账文件名
LEDGER_FILE = "ledger.jsonl"

#: 单文件上限（MB）。超过后轮转为 `ledger.<时间戳>.jsonl`，
#: 避免一个文件无限增长导致统计时全量读入内存。
MAX_FILE_MB = 64.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _epoch(at: str) -> float:
    try:
        return datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _to_int_safe(value: object, default: int = 0) -> int:
    """容错整数（统计字段可能缺失/为 None/为字符串）。"""
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError, OverflowError):
        return default


def _to_float_safe(value: object, default: float = 0.0) -> float:
    """容错浮点（同上）。"""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default


@dataclass
class LedgerEntry:
    """一条决策留痕（一个盘口的一个结果）。"""

    #: 决策时刻（ISO8601 UTC）
    at: str
    match_id: str
    decision_id: str = ""
    updated_at: str = ""
    competition_type: str = "unknown"
    is_live: bool = False
    entry_score: Optional[List[int]] = None
    entry_clock_s: Optional[float] = None
    settlement_basis: str = "legacy_full_score"
    model_version: str = "legacy"
    league: str = ""
    home: str = ""
    away: str = ""
    market: str = ""
    line: str = ""
    outcome: str = ""
    #: 人话标签（如「上半场大1.5」）
    label: str = ""
    odds: float = 0.0
    #: 该结果是否被 LLM 判为可买入
    is_pick: bool = False
    #: 未通过门控时的原因码（`is_pick=False` 时有意义）
    rejects: List[str] = field(default_factory=list)
    #: 动态门槛（仅 pick 有意义）
    required_edge: float = 0.0
    edge: float = 0.0
    confidence: float = 0.0
    p_market: float = 0.0
    p_llm: float = 0.0
    p_fused: float = 0.0
    llm_weight: float = 0.0
    kelly: float = 0.0
    trend: str = ""
    trend_pct: float = 0.0
    #: 该场决策时 LLM 是否真的被调用（`no_llm` 与超时必须可区分）
    llm_used: bool = False
    llm_confidence: float = 0.0
    decision: str = ""
    #: 触发来源：`price_change` / `cycle` / `manual`
    trigger: str = ""
    # -- 结算字段 --------------------------------------------------------
    status: str = SETTLE_PENDING
    #: 每单位本金的净收益（`status` 非终结时为 0）
    pnl: float = 0.0
    ft_score: Optional[List[int]] = None
    ht_score: Optional[List[int]] = None
    #: 收盘赔率（用于 CLV）；结算时记录
    closing_odds: float = 0.0
    settled_at: str = ""
    settle_note: str = ""

    @property
    def key(self) -> tuple:
        """同一盘口结果的唯一键（用于取最新一条）。"""
        return ((self.decision_id,) + self.quote_key
                if self.decision_id else self.quote_key)

    @property
    def quote_key(self) -> tuple:
        return (self.match_id, self.market, self.line, self.outcome)

    @property
    def date_key(self) -> str:
        """决策日期 `YYYY-MM-DD`（本地分组用；`at` 为 ISO8601 UTC）。

        为何不直接按 `at`（含时分秒）分组：那样每条都是独立一组，
        等于没有聚合。按日聚合才能看出“哪几天在赚钱”。
        """
        return str(self.at or "")[:10]

    @property
    def clv(self) -> Optional[float]:
        return (clv(self.odds, self.closing_odds)
                if self.closing_odds and not self.is_live else None)

    def as_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["clv"] = self.clv
        d["price_drift"] = clv(self.odds, self.closing_odds) if self.closing_odds else None
        d["legacy_identity"] = not bool(self.decision_id)
        # 用户要求：盘口信息一律以中文展示，与乐鱼一致
        # （如「曼联上半场-1」「上半场进球数>1/1.5」）。
        # `label` 只在是买入建议时被写入（取自 picks），历史条目可能为空，
        # 所以这里统算一份，保证每一条都能直接看懂。
        try:
            from core.market_labels import format_market
            d["label"] = format_market(self.market, self.outcome, self.line,
                                      home=self.home, away=self.away)
            d["market_label"] = describe_market(self.market)
        except Exception:  # noqa: BLE001 - 标签是展示增强，不得影响统计
            pass
        return d

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "LedgerEntry":
        """从落盘 JSON 还原；未知字段忽略，缺失字段用默认值。"""
        names = {f for f in cls.__dataclass_fields__}
        kw = {k: v for k, v in raw.items() if k in names}
        if kw.get("competition_type", "unknown") == "unknown":
            from core.live_model import competition_type
            kw["competition_type"] = competition_type(kw)
        # 类型兜底：脏数据不应让整次统计失败
        for num in ("odds", "required_edge", "edge", "confidence",
                    "p_market", "p_llm", "p_fused", "llm_weight",
                    "kelly", "trend_pct", "llm_confidence", "pnl",
                    "closing_odds"):
            if num in kw:
                try:
                    value = float(kw[num] or 0.0)
                    kw[num] = value if math.isfinite(value) else 0.0
                except (TypeError, ValueError):
                    kw[num] = 0.0
        if "rejects" in kw and not isinstance(kw["rejects"], list):
            kw["rejects"] = []
        return cls(**kw)


class DecisionLedger:
    """决策台账（线程安全，追加写 JSONL）。"""

    def __init__(self, root: Optional[str | Path] = None) -> None:
        self.root = Path(root) if root else None
        self._lock = threading.RLock()
        self._loaded_stamp: tuple = ()
        self._loaded_rows: List[LedgerEntry] = []
        self._warned = ""
        self.last_error = ""
        if self.root is not None:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.last_error = "创建台账目录失败: %s" % exc
                self.root = None

    # -- 路径与轮转 ------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.root is not None

    @property
    def path(self) -> Optional[Path]:
        return (self.root / LEDGER_FILE) if self.root else None

    def _rotate_if_needed(self, path: Path) -> None:
        """文件超过上限则改名归档（调用方持锁）。"""
        try:
            if not path.exists():
                return
            if path.stat().st_size < MAX_FILE_MB * 1024 * 1024:
                return
            stamp = _now().strftime("%Y%m%d-%H%M%S")
            path.rename(path.with_name("ledger.%s.jsonl" % stamp))
        except OSError as exc:
            self.last_error = "台账轮转失败: %s" % exc

    def _append(self, rows: Sequence[LedgerEntry]) -> int:
        path = self.path
        if path is None or not rows:
            return 0
        with self._lock:
            self._rotate_if_needed(path)
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    for r in rows:
                        fh.write(json.dumps(r.as_dict(), ensure_ascii=False))
                        fh.write("\n")
                self._loaded_stamp = ()
                return len(rows)
            except (OSError, TypeError, ValueError) as exc:
                self.last_error = "台账写入失败: %s" % exc
                return 0

    # -- 读取 ------------------------------------------------------------

    def _files(self) -> List[Path]:
        if self.root is None:
            return []
        try:
            return sorted(self.root.glob("ledger*.jsonl"))
        except OSError as exc:
            self.last_error = "扫描台账目录失败: %s" % exc
            return []

    def load(self) -> List[LedgerEntry]:
        """读全部台账（含轮转归档），按 `key` 去重取**最新一条**。"""
        with self._lock:
            return self._load_locked()

    def _load_locked(self) -> List[LedgerEntry]:
        files = self._files()
        try:
            stamp = tuple((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in files)
        except OSError:
            stamp = ()
        if stamp and stamp == self._loaded_stamp:
            return [replace(e) for e in self._loaded_rows]
        latest: Dict[tuple, LedgerEntry] = {}
        for path in files:
            try:
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            raw = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(raw, Mapping):
                            continue
                        try:
                            e = LedgerEntry.from_dict(raw)
                        except (TypeError, ValueError):
                            continue
                        # 后写的覆盖先写的（文件按名排序 ≈ 时间排序）
                        prev = latest.get(e.key)
                        if prev is None or _epoch(e.updated_at or e.settled_at or e.at) >= _epoch(
                                prev.updated_at or prev.settled_at or prev.at):
                            latest[e.key] = e
            except OSError as exc:
                self.last_error = "读取 %s 失败: %s" % (path.name, exc)
        rows = sorted(latest.values(), key=lambda e: _epoch(e.at))
        self._loaded_stamp = stamp
        self._loaded_rows = rows
        return [replace(e) for e in rows]

    # -- 记录决策 --------------------------------------------------------

    @staticmethod
    def _f(value: Any) -> float:
        """安全取浮点：决策结果里的字段可能缺失/为 None/为字符串。

        台账是留痕，宁可写 0 也不能因一个字段缺失就丢掉整场记录。
        """
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def record_match(self, result: Any, trigger: str = "") -> int:
        """把一场的决策结果写入台账。

        Args:
            result: `MatchPicks`（含 `computations` 与 `picks`）。
            trigger: 触发来源（`price_change` / `cycle` / `manual`）。

        Returns:
            写入条数。
        """
        if not self.enabled:
            return 0
        mid = str(getattr(result, "match_id", "") or "")
        if not mid:
            return 0
        at = _now().isoformat()
        decision_id = uuid.uuid4().hex
        picks = {self._pick_key(p): p
                 for p in (getattr(result, "picks", None) or [])}
        rows: List[LedgerEntry] = []
        for comp in (getattr(result, "computations", None) or []):
            market = str(getattr(comp, "market", "") or "")
            line = str(getattr(comp, "line", "") or "")
            odds = tuple(getattr(comp, "odds", ()) or ())
            outcomes = tuple(getattr(comp, "outcomes", ()) or ())
            for i, oc in enumerate(outcomes):
                key = (market, line, str(oc))
                pick = picks.get(key)
                rows.append(LedgerEntry(
                    at=at, match_id=mid, decision_id=decision_id, updated_at=at,
                    league=str(getattr(result, "league", "") or ""),
                    home=str(getattr(result, "home", "") or ""),
                    away=str(getattr(result, "away", "") or ""),
                    market=market, line=line, outcome=str(oc),
                    label=str((pick or {}).get("pick_label") or ""),
                    odds=self._f(odds[i]) if i < len(odds) else 0.0,
                    is_pick=pick is not None,
                    rejects=list(getattr(comp, "reject_reasons", []) or [])
                    if pick is None else [],
                    required_edge=self._f((pick or {}).get("required_edge")),
                    edge=self._f((pick or {}).get("edge")),
                    confidence=self._f((pick or {}).get("confidence")),
                    p_market=(self._f((pick or {}).get("p_market"))
                              or self._p_fair(comp, i)),
                    p_llm=self._f((pick or {}).get("p_llm")),
                    p_fused=self._f((pick or {}).get("p_fused")),
                    llm_weight=self._f((pick or {}).get("llm_weight")),
                    kelly=self._f((pick or {}).get("kelly")),
                    trend=str(getattr(comp, "trend", "") or ""),
                    trend_pct=self._f(getattr(comp, "trend_pct", 0.0)),
                    llm_used=bool(getattr(result, "llm_used", False)),
                    llm_confidence=self._f(getattr(result, "llm_confidence", 0.0)),
                    decision=str(getattr(result, "decision", "") or ""),
                    trigger=trigger,
                ))
        # 只留**买入建议**与「被门控拦掉但算过 edge」的盘口，避免台账爆炸。
        # 全部盘口都写会让行数变成 50 倍，且多数毫无信息量。
        keep = [r for r in rows if r.is_pick or r.rejects]
        return self._append(keep)

    @staticmethod
    def _pick_key(pick: Mapping[str, Any]) -> tuple:
        return (str(pick.get("market") or ""), str(pick.get("line") or ""),
                str(pick.get("outcome") or ""))

    @staticmethod
    def _p_fair(comp: Any, i: int) -> float:
        """取盘口第 i 个结果的公平概率（去水后）。

        为何要容错：`comp` 可能来自反序列化/测试替身，`p_fair` 元素
        可能是字符串或缺失。一个脏字段不应让整份台账统计崩掉
        （那样用户就永远拿不到命中率）。
        """
        fair = tuple(getattr(comp, "p_fair", None) or ())
        if i >= len(fair):
            return 0.0
        try:
            return float(fair[i])
        except (TypeError, ValueError, OverflowError):
            return 0.0

    # -- 结算 ------------------------------------------------------------

    def capture_closing(self, quotes: Mapping[tuple, float]) -> int:
        """把**收盘赔率**写入待结算条目（CLV 的前置条件）。

        为何必须单独立这个方法（本项目真实缺口）：
        CLV = 买入赔率 / 收盘赔率 − 1，仅在同一信息集下可比较；不能称为**唯一领先指标**
        （见 `core/entry_gate.py` 证据 [E]）。但它**不能事后重建** ——
        必须在下注当时就把参照的收盘价记下来。原实现只在 `settle()`
        里从 `scores` 的可选 `closing` 字段取，而 `settle_finished()`
        从不填它，于是 `closing_odds` 永远是 0、`clv_mean` 恒为 None：
        用户问“LLM 准不准”时，最有用的那个指标根本算不出来。

        做法：把新价追加写到同一 key（`load()` 按 `at` 取最新一条），
        **只更新仍为 pending 的条目** —— 已结算条目带终场结论，
        若被覆盖会把状态改回 pending，导致统计回退。

        Args:
            quotes: `(match_id, market, line, outcome) -> 收盘赔率`。

        Returns:
            实际更新的条数。
        """
        if not self.enabled or not quotes:
            return 0
        with self._lock:
            updated: List[LedgerEntry] = []
            for e in self.load():
                if e.status != SETTLE_PENDING:
                    continue
                q = quotes.get(e.quote_key)
                try:
                    qf = float(q)  # type: ignore[arg-type]
                except (TypeError, ValueError, OverflowError):
                    continue
                if not math.isfinite(qf) or qf <= 1 or abs(qf - e.closing_odds) < 1e-9:
                    continue
                e.closing_odds = qf
                e.updated_at = _now().isoformat()
                updated.append(e)
            return self._append(updated)

    def pending_match_ids(self) -> List[str]:
        """仍待结算的赛事 ID（去重）——用于定向补齐收盘赔率。"""
        out: List[str] = []
        seen: set = set()
        for e in self.load():
            if e.status != SETTLE_PENDING or not e.match_id:
                continue
            if e.match_id not in seen:
                seen.add(e.match_id)
                out.append(e.match_id)
        return out

    def settle(self, scores: Mapping[str, Any], regrade: bool = False) -> Dict[str, int]:
        """用终场比分结算台账里的待结算条目。

        Args:
            scores: `match_id -> {"ft": (主,客), "ht": (主,客)}`；
                也接受 `match_id -> (主,客)`（视为全场比分，半场盘口转 void）。

        Returns:
            统计字典 `{scanned, settled, void, skipped}`。
        """
        if not self.enabled:
            return {"scanned": 0, "settled": 0, "void": 0, "skipped": 0}
        with self._lock:
            return self._settle_locked(scores, regrade)

    def _settle_locked(self, scores: Mapping[str, Any], regrade: bool) -> Dict[str, int]:
        entries = self.load()
        out = {"scanned": 0, "settled": 0, "void": 0, "skipped": 0}
        updates: List[LedgerEntry] = []
        for e in entries:
            out["scanned"] += 1
            if e.status in TERMINAL_STATUSES and not regrade:
                continue
            sc = scores.get(e.match_id)
            if isinstance(sc, Mapping) and sc.get("done") is False:
                out["skipped"] += 1
                continue
            ft, ht = self._split_score(sc)
            if ft is None:
                out["skipped"] += 1
                continue
            grade_ft, grade_ht = ft, ht
            if e.settlement_basis == "remaining_score":
                from core.settlement import _score_of
                entry = _score_of(e.entry_score)
                if entry is None or ft[0] < entry[0] or ft[1] < entry[1]:
                    out["skipped"] += 1
                    continue
                grade_ft = (ft[0] - entry[0], ft[1] - entry[1])
                if ht is not None and ht[0] >= entry[0] and ht[1] >= entry[1]:
                    grade_ht = (ht[0] - entry[0], ht[1] - entry[1])
            status, note = settle_pick(e.market, e.outcome, e.line, grade_ft, grade_ht)
            if e.settlement_basis == "unknown" or not math.isfinite(e.odds) or e.odds <= 1:
                status, note = SETTLE_VOID, "结算口径或入场赔率未核验"
            e.status, e.settle_note = status, note
            e.ft_score = list(ft)
            e.ht_score = list(ht) if ht is not None else None
            e.settled_at = _now().isoformat()
            e.updated_at = e.settled_at
            e.pnl = round(pnl_for(status, e.odds), 6) if e.is_pick else 0.0
            close = self._closing_of(sc, e)
            if close is not None:
                e.closing_odds = close
            out["void" if status == SETTLE_VOID else "settled"] += 1
            updates.append(e)
        # Append revisions; archived originals cannot overwrite a newer settlement.
        self._append(updates)
        return out

    @staticmethod
    def _split_score(sc: Any) -> tuple:
        """把 scores 值统一成 `(ft, ht)`。"""
        from core.settlement import _score_of
        if isinstance(sc, Mapping):
            return _score_of(sc.get("ft")), _score_of(sc.get("ht"))
        return _score_of(sc), None

    @staticmethod
    def _closing_of(sc: Any, e: LedgerEntry) -> Optional[float]:
        """从 scores 附带的收盘赔率里取该条目的值（若提供）。"""
        if not isinstance(sc, Mapping):
            return None
        closes = sc.get("closing")
        if not isinstance(closes, Mapping):
            return None
        row = closes.get("|".join(str(x) for x in e.quote_key))
        if row is None:
            return None
        try:
            v = float(row)
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) and v > 1.0 else None

    def _rewrite(self, rows: Sequence[LedgerEntry]) -> None:
        """原子重写台账（临时文件 + rename）。"""
        path = self.path
        if path is None:
            return
        with self._lock:
            # 用 with_name 而不是 with_suffix：文件名是 `ledger.jsonl`，
            # with_suffix(".tmp") 会把它改成 `ledger.tmp`（丢掉 .jsonl），
            # 虽然此处可用，但语义不对且容易被后续改动误伤。
            tmp = path.with_name(path.name + ".tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    for r in rows:
                        fh.write(json.dumps(r.as_dict(), ensure_ascii=False))
                        fh.write("\n")
                os.replace(tmp, path)
                self._loaded_stamp = ()
            except (OSError, TypeError, ValueError) as exc:
                self.last_error = "台账重写失败: %s" % exc

    # -- 统计 ------------------------------------------------------------

    def stats(self, only_picks: bool = True,
              trigger: Optional[str] = None) -> Dict[str, Any]:
        """本地统计：命中率 / ROI / CLV。

        Args:
            only_picks: 只统计「买入建议」（默认）。设为 False 可看全部
                记录（含被门控拦截的），用于校准门控阈值。
            trigger: 只看某个触发来源（`price_change` / `cycle`）。
        """
        rows = self.load()
        if only_picks:
            rows = [r for r in rows if r.is_pick]
        if trigger:
            rows = [r for r in rows if r.trigger == trigger]
        base = summarise(rows)
        # 未结算的单独给个提示（用户最关心「还要等多久才有结论」）
        base["legacy_identity_rows"] = sum(not r.decision_id for r in rows)
        base["by_type"] = self._group_by(rows, "competition_type")
        base["by_trigger"] = self._group_by(rows, "trigger")
        base["by_decision"] = self._group_by(rows, "decision")
        base["pending_matches"] = sorted(
            {r.match_id for r in rows if r.status == SETTLE_PENDING})
        return base

    @staticmethod
    def _group_by(rows: Sequence[LedgerEntry], attr: str) -> Dict[str, Any]:
        buckets: Dict[str, List[LedgerEntry]] = {}
        for r in rows:
            buckets.setdefault(str(getattr(r, attr, "") or ""), []).append(r)
        return {k: summarise(v) for k, v in buckets.items()}

    # -- 历史战绩（用户要求：展示历史决策与实际结果的统计） ---------------

    def history(
        self,
        picks_only: bool = True,
        limit: int = 300,
        days: int = 0,
        competition_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """历史决策 vs 实际结果的**分组统计 + 明细**。

        与 `stats()` 的区别（两者互补，不是重复）：
          * `stats()` —— 只给**总量**指标（命中率/ROI/CLV），
            用于“模型到底行不行”的一句话结论；
          * `history()` —— 拆分到**时间/联赛/盘口/决策来源**，
            并回传逐条明细（含实际比分），用于“为什么不行、哪一类不行”。

        为何需要分组（否则看不出可优化点）：
        总命中率 52% 可能掩盖“某个联赛 20%”或“某个盘口 80%”
        —— 后者才是可执行的改进信号。

        Args:
            picks_only: 只看买入建议（默认）。设 False 可同时看被门控
                拦截的盘口表现，用于校准门控阈值（这是判断门控是在
                帮忙还是在误杀的唯一依据）。
            limit: 明细返回条数上限（默认 300）。
            days: 只看最近 N 天；0 表示不限。

        Returns:
            含 `overall` / `by_date` / `by_league` / `by_market` /
            `by_decision` / `by_trigger` / `by_status` / `entries` /
            `timeline` 的字典，全部**可在容器内复核**，不依赖外部服务。
        """
        rows = self.load()
        if picks_only:
            rows = [r for r in rows if r.is_pick]
        if competition_type and competition_type != "all":
            rows = [r for r in rows if r.competition_type == competition_type]
        if days and days > 0:
            cutoff = (_now() - timedelta(days=days)).isoformat()
            # `at` 是 ISO8601 UTC 字符串，字典序即时间序（同格式可比）
            rows = [r for r in rows if str(r.at) >= cutoff]

        # 明细：**已结算优先且按时间倒序**，让人先看到有结论的
        def _rank(r: LedgerEntry) -> Any:
            settled = r.status not in (SETTLE_PENDING,)
            return (0 if settled else 1, -_epoch(r.at))
        ordered = sorted(rows, key=_rank)
        entries = [r.as_dict() for r in ordered[:max(0, limit)]]

        graded = [r for r in rows
                  if r.status in GRADED_STATUSES]
        return {
            "overall": summarise(rows),
            "settled": summarise(graded),
            "legacy_identity_rows": sum(not r.decision_id for r in rows),
            "by_type": self._group_by(rows, "competition_type"),
            "by_date": self._group_by(rows, "date_key"),
            "by_league": self._group_by(rows, "league"),
            "by_market": self._group_by(rows, "market"),
            "by_decision": self._group_by(rows, "decision"),
            "by_trigger": self._group_by(rows, "trigger"),
            "by_status": self._count_by(rows, "status"),
            "timeline": self._timeline(graded),
            "entries": entries,
            "entries_shown": len(entries),
            "entries_total": len(rows),
            "picks_only": bool(picks_only),
            "days": days or 0,
            "ledger": self.health(),
        }

    @staticmethod
    def _count_by(rows: Sequence[LedgerEntry], attr: str) -> Dict[str, int]:
        """按字段计数（不跑完整 summarise，避免无意义的指标计算）。"""
        out: Dict[str, int] = {}
        for r in rows:
            k = str(getattr(r, attr, "") or "")
            out[k] = out.get(k, 0) + 1
        return out

    @staticmethod
    def _timeline(graded: Sequence[LedgerEntry]) -> List[Dict[str, Any]]:
        """按**决策日期**聚合的“累计结果”序列，用于看命中率走势。

        为何要累计列：单日样本往往只有几注，日命中率跳动极大（0%↔100%），
        看累计曲线才能判断模型是否真的在赚钱，而不是被小样本噪声骗。

        ⚠️ **按决策日期（`at`）而不是结算日期分组**（本项目测试抓到的设计错）：
        结算是**批量任务**（每 `settle_interval_s` 跑一轮），
        同一批历史条目会在**同一天**被集中结算 —— 若按 `settled_at` 分组，
        整条曲线会堆到“今天”一列，完全看不出历史分布。
        按决策日期分组才是稳定的，也贴合“历史决策 vs 实际结果”的语义。
        """
        buckets: Dict[str, List[LedgerEntry]] = {}
        for r in graded:
            day = str(r.at or "")[:10]
            buckets.setdefault(day, []).append(r)
        out: List[Dict[str, Any]] = []
        cum_stake = 0
        cum_profit = 0.0
        for day in sorted(buckets):
            st = summarise(buckets[day])
            stake = _to_int_safe(st.get("stake_units"))
            profit = _to_float_safe(st.get("profit_units"))
            cum_stake += stake
            cum_profit += profit
            out.append({
                "date": day,
                "n": st.get("graded") or 0,
                "won": st.get("won") or 0,
                "lost": st.get("lost") or 0,
                "hit_rate": st.get("hit_rate"),
                "profit_units": round(profit, 4),
                "cum_stake": cum_stake,
                "cum_profit": round(cum_profit, 4),
                "cum_roi": (round(cum_profit / cum_stake, 6)
                            if cum_stake else None),
            })
        return out

    def health(self) -> Dict[str, Any]:
        path = self.path
        size = 0
        if path is not None and path.exists():
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
        return {
            "enabled": self.enabled,
            "path": str(path) if path else "",
            "files": len(self._files()),
            "size_bytes": size,
            "last_error": self.last_error,
        }
