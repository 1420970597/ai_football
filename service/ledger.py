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
（避免半写文件）；统计时按 `(match_id, market, line, outcome)` 取
**最新一条**（同一盘口会因盘口变动被多次决策，取最后一次才有意义）。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from core.settlement import (
    SETTLE_PENDING,
    SETTLE_VOID,
    TERMINAL_STATUSES,
    clv,
    pnl_for,
    settle_pick,
    summarise,
)

__all__ = ["LedgerEntry", "DecisionLedger"]

#: 台账文件名
LEDGER_FILE = "ledger.jsonl"

#: 单文件上限（MB）。超过后轮转为 `ledger.<时间戳>.jsonl`，
#: 避免一个文件无限增长导致统计时全量读入内存。
MAX_FILE_MB = 64.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class LedgerEntry:
    """一条决策留痕（一个盘口的一个结果）。"""

    #: 决策时刻（ISO8601 UTC）
    at: str
    match_id: str
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
        return (self.match_id, self.market, self.line, self.outcome)

    @property
    def clv(self) -> Optional[float]:
        return clv(self.odds, self.closing_odds) if self.closing_odds else None

    def as_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["clv"] = self.clv
        return d

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "LedgerEntry":
        """从落盘 JSON 还原；未知字段忽略，缺失字段用默认值。"""
        names = {f for f in cls.__dataclass_fields__}
        kw = {k: v for k, v in raw.items() if k in names}
        # 类型兜底：脏数据不应让整次统计失败
        for num in ("odds", "required_edge", "edge", "confidence",
                    "p_market", "p_llm", "p_fused", "llm_weight",
                    "kelly", "trend_pct", "llm_confidence", "pnl",
                    "closing_odds"):
            if num in kw:
                try:
                    kw[num] = float(kw[num] or 0.0)
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
        latest: Dict[tuple, LedgerEntry] = {}
        for path in self._files():
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
                        if prev is None or e.at >= prev.at:
                            latest[e.key] = e
            except OSError as exc:
                self.last_error = "读取 %s 失败: %s" % (path.name, exc)
        return sorted(latest.values(), key=lambda e: e.at)

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
                    at=at, match_id=mid,
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
        fair = tuple(getattr(comp, "p_fair", ()) or ())
        return float(fair[i]) if i < len(fair) else 0.0

    # -- 结算 ------------------------------------------------------------

    def settle(self, scores: Mapping[str, Any]) -> Dict[str, int]:
        """用终场比分结算台账里的待结算条目。

        Args:
            scores: `match_id -> {"ft": (主,客), "ht": (主,客)}`；
                也接受 `match_id -> (主,客)`（视为全场比分，半场盘口转 void）。

        Returns:
            统计字典 `{scanned, settled, void, skipped}`。
        """
        if not self.enabled:
            return {"scanned": 0, "settled": 0, "void": 0, "skipped": 0}
        entries = self.load()
        out = {"scanned": 0, "settled": 0, "void": 0, "skipped": 0}
        # 重写整个台账：结算字段需要原地更新，追加写做不到。
        # 用「重写全部」而非「追加结算行」是为了让统计逻辑简单可靠
        # （追加会造成同一 key 多行、状态判定复杂）。
        rewritten: List[LedgerEntry] = []
        for e in entries:
            out["scanned"] += 1
            if e.status in TERMINAL_STATUSES:
                rewritten.append(e)
                continue
            sc = scores.get(e.match_id)
            if sc is None:
                out["skipped"] += 1
                rewritten.append(e)
                continue
            ft, ht = self._split_score(sc)
            if ft is None:
                out["skipped"] += 1
                rewritten.append(e)
                continue
            status, note = settle_pick(e.market, e.outcome, e.line, ft, ht)
            e.status = status
            e.settle_note = note
            e.ft_score = list(ft) if ft else None
            e.ht_score = list(ht) if ht else None
            e.settled_at = _now().isoformat()
            e.pnl = round(pnl_for(status, e.odds), 6) if e.is_pick else 0.0
            # 收盘赔率：用结算时点的最新赔率（调用方在 scores 里带上来）
            close = self._closing_of(sc, e)
            if close is not None:
                e.closing_odds = close
            if status == SETTLE_VOID:
                out["void"] += 1
            else:
                out["settled"] += 1
            rewritten.append(e)
        self._rewrite(rewritten)
        return out

    @staticmethod
    def _split_score(sc: Any) -> tuple:
        """把 scores 值统一成 `(ft, ht)`。"""
        if isinstance(sc, Mapping):
            ft = sc.get("ft")
            ht = sc.get("ht")
            return (tuple(ft) if isinstance(ft, (list, tuple)) else None,
                    tuple(ht) if isinstance(ht, (list, tuple)) else None)
        if isinstance(sc, (list, tuple)) and len(sc) >= 2:
            return (tuple(sc[:2]), None)
        return (None, None)

    @staticmethod
    def _closing_of(sc: Any, e: LedgerEntry) -> Optional[float]:
        """从 scores 附带的收盘赔率里取该条目的值（若提供）。"""
        if not isinstance(sc, Mapping):
            return None
        closes = sc.get("closing")
        if not isinstance(closes, Mapping):
            return None
        row = closes.get("|".join(str(x) for x in e.key))
        if row is None:
            return None
        try:
            v = float(row)
        except (TypeError, ValueError):
            return None
        return v if v > 1.0 else None

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
