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
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from collector.leyu_client import DEFAULT_HOST, DEFAULT_ORIGIN
from collector.normalizer import normalize_matches
from collector.sources import make_source
from core import calibration as cal
from core import devig as devig_mod
from core import economics as econ
from core import markets as mk
from core import microstructure as micro
from core.models import (
    DevigMethod,
    FairProbabilities,
    OddsSnapshot,
    SnapshotState,
    utcnow,
)
from store import SnapshotStore, make_cache, safe_name

__all__ = ["ValuationService", "load_corpus"]

#: 乐鱼源写入快照的展示名（现为**默认**数据源）
LEYU_SOURCE_NAME = "乐鱼API"

#: 体彩源展示名（保留作为可选数据源与回归对照）
DEFAULT_SOURCE = "体彩官方API"


def _env_int(name: str, default: int) -> int:
    """读整数环境变量；缺失或非法时回退 default。"""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


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
    """估值服务：快照 → 去水 → 优势 → 仓位 → 校准。

    数据源是可插拔的（见 `collector.sources`）：默认 **乐鱼（leyu）**，
    可通过 `DATA_SOURCE` 环境变量或构造参数切换，体彩文件源保留为可选。
    """

    def __init__(
        self,
        snapshot_root: str | Path,
        corpus_root: Optional[str | Path] = None,
        cache: Optional[Any] = None,
        prefer_redis: Optional[bool] = None,
        source: Optional[str] = None,
        saz_path: Optional[str | Path] = None,
        source_obj: Optional[Any] = None,
    ) -> None:
        """构造估值服务。

        prefer_redis：缓存后端偏好。
          - None（默认）：根据环境变量自动判定 —— 设了 REDIS_URL 就尝试 Redis，
            否则用内存缓存。**不应硬编码为 False**，否则容器里永远连不上 Redis，
            缓存不跨进程（这是一个曾经真实存在的缺陷）。
          - True / False：显式指定（供测试使用）。

        source：数据源名称（`leyu` / `ticai`，支持中文别名）。
          - None（默认）：读 `DATA_SOURCE` 环境变量；仍为空则用 **leyu**。
        saz_path：乐鱼抓包路径；给出时乐鱼源进入**离线回放**（无需联网）。
        source_obj：直接注入已构造的数据源（供测试/扩展覆盖前两者）。
        """
        if prefer_redis is None:
            prefer_redis = bool(os.environ.get("REDIS_URL", "").strip()) \
                and os.environ.get("CACHE_BACKEND", "").strip().lower() != "memory"
        self.store = SnapshotStore(
            snapshot_root,
            cache=cache if cache is not None else make_cache(
                prefer_redis=prefer_redis),
        )
        self.corpus_root = Path(corpus_root) if corpus_root else None
        self.saz_path = str(saz_path) if saz_path else \
            (os.environ.get("LEYU_SAZ") or None)
        self._ingested = False

        if source_obj is not None:
            self.source = source_obj
        else:
            resolved = source if source is not None \
                else (os.environ.get("DATA_SOURCE") or None)
            self.source = make_source(
                resolved,
                corpus_root=self.corpus_root,
                # 仅在显式给出 saz 时回放；否则与乐鱼在线网关打交道
                saz_path=self.saz_path,
                host=os.environ.get("LEYU_HOST") or DEFAULT_HOST,
                origin=os.environ.get("LEYU_ORIGIN") or DEFAULT_ORIGIN,
                request_id=os.environ.get("LEYU_REQUEST_ID") or None,
                batch_size=_env_int("LEYU_BATCH", 20),
            )

    # -- 数据准备 -----------------------------------------------------------

    def ingest_corpus(
        self,
        source: Optional[str] = None,
        mids: Optional[Sequence[str]] = None,
        max_matches: Optional[int] = None,
        full: bool = False,
        progress: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """拉取数据源 → 归一化 → 写入不可变快照存储。

        Args:
            source: 覆盖数据源展示名（默认用数据源自身的 `display_source`）。
            mids: 限定赛事 ID（乐鱼源的两阶段拉取必需，否则会拉上千场）。
            max_matches: 上限；未指定且 full=False 时乐鱼源默认 60 场。
            full: 全量采集（不截断）。实测约 2233 场 / 111 批 / ~40 秒。
            progress: 可选进度回调 `fn(done, total)`。
        """
        snaps, issues = self._fetch(source, mids, max_matches, full, progress)
        written = self.store.append_many(snaps)
        self._ingested = True
        return {
            "data_source": self.source.name,
            "snapshots": len(snaps),
            "written": len(written),
            "issues": len(issues),
            "issue_samples": issues[:20],
        }

    def _fetch(
        self,
        source: Optional[str],
        mids: Optional[Sequence[str]],
        max_matches: Optional[int],
        full: bool = False,
        progress: Optional[Any] = None,
    ) -> Any:
        """按数据源类型分派拉取（保留体彩源的 source 覆盖能力）。"""
        kwargs: Dict[str, Any] = {"mids": mids, "max_matches": max_matches}
        # 仅乐鱼源支持 full / progress；体彩源忽略这两个参数
        if self.source.name == "leyu":
            kwargs["full"] = full
            kwargs["progress"] = progress
        if source is None or source == self.source.display_source:
            return self.source.fetch(**kwargs)
        # 显式覆盖：仅体彩文件源支持自定义展示名
        snaps, issues = self.source.fetch(**kwargs)
        if self.source.name == "ticai":
            return [replace(s, source=source) for s in snaps], issues
        return snaps, issues

    def _all_snapshots(self) -> List[OddsSnapshot]:
        """扫描存储，返回**当前数据源**的全部快照。

        重要：存储按 `<source>/<league>/` 分目录。切换数据源后，
        旧源（如体彩）的历史快照仍在磁盘上；若不加过滤地全扫，
        新旧数据会混合，页面会出现「已切换数据源但仍是旧数据」的假象。
        因此这里只读当前数据源目录。

        回退：当前数据源目录不存在时（如刚切换、尚未采集），
        返回空列表而不是旧源数据 —— 宁可先显示空，也不要展示错误来源的数据。
        """
        base = self.store.root / safe_name(self.source.display_source)
        if not base.exists():
            return []
        out: List[OddsSnapshot] = []
        for d in sorted(base.rglob("_index.json")):
            out.extend(self.store._load_dir(d.parent))
        return out

    # -- 采集自动落库 -------------------------------------------------------

    def ensure_fresh(
        self,
        max_matches: Optional[int] = None,
        max_age_s: float = 900.0,
        full: bool = False,
    ) -> Dict[str, Any]:
        """若当前数据源的存储为空或过期，则自动采集一次。

        用途：让 Web 控制台在首次打开时就能看到当前数据源的数据，
        而不是上次遗留的旧快照。

        Args:
            max_matches: 单次采集上限（透传给数据源）。
            max_age_s: 快照最大可接受年龄（秒）；超过则重新采集。
            full: 全量采集（用于「赛事必须完整」的刷库场景）。

        Returns:
            `ingest_corpus()` 的结果，或 `{"skipped": ...}` 说明为何未采集。
        """
        # 全量模式下不能因为「有旧数据」就跳过：需要补齐全部赛事
        if not full:
            snaps = self._all_snapshots()
            if snaps:
                newest = max(s.captured_at for s in snaps)
                age = (utcnow() - newest).total_seconds()
                if age <= max_age_s:
                    return {"skipped": "数据仍新鲜", "age_s": round(age, 1),
                            "snapshots": len(snaps)}
        res = self.ingest_corpus(max_matches=max_matches, full=full)
        res["auto"] = True
        return res

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

    #: 遗留市场名 → 规范市场名（既有 output JSON 用 1X2/AH，
    #: 新目录用 HAD/HHAD(line)。不做这层映射会导致
    #: 「模型 vs 市场」对照永远找不到快照，从而静默给出无对照的结果。
    _MARKET_ALIASES: Mapping[str, Tuple[str, ...]] = {
        "1X2": ("1X2", "HAD"),
        "HAD": ("HAD", "1X2"),
        "AH": ("AH", "HHAD(-1)", "HHAD(-1.0)"),
    }

    def _candidate_names(self, market: str) -> Tuple[str, ...]:
        """给定市场名，返回应尝试匹配的全部等价名（含遗留名）。"""
        name = market.strip()
        out: List[str] = [name]
        for extra in self._MARKET_ALIASES.get(name, ()):
            if extra not in out:
                out.append(extra)
        # 让球类：HHAD(-1) 与旧 AH 互为等价
        if name.startswith("HHAD"):
            for legacy in ("AH", "HHAD(-1)", "HHAD(-1.0)"):
                if legacy not in out:
                    out.append(legacy)
        if name.startswith("OU"):
            if "OU" not in out:
                out.append("OU")
        return tuple(out)

    def _latest(self, match_id: str,
                market: str = "1X2",
                fallback: bool = False) -> Optional[OddsSnapshot]:
        """取某市场的最新快照。

        Args:
            market: 市场名（支持遗留名与规范名的等价匹配）。
            fallback: 精确匹配失败时，是否降级到任意可用市场。
                乐鱼源并非每场都有胜平负（部分只有让球/大小），
                而控制台默认请求 1X2/HAD；不开降级会导致这些场次
                的赔率列、水钱、公平概率全为空。
        """
        names = self._candidate_names(market)
        snaps = self._snapshots_of(match_id)
        # 先按规范名精确匹配，再退回等价名，保证新数据优先
        for name in names:
            for s in reversed(snaps):
                if s.market == name:
                    return s
        if fallback and snaps:
            # 降级：优先两结果市场（让球/大小），否则取任一
            for s in reversed(snaps):
                if s.market.startswith(("AH", "OU")):
                    return s
            return snaps[-1]
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

    #: 规范市场名（把遗留名折叠到新目录名，便于跨玩法校验对得上）
    def _canonical_market(self, market: str) -> str:
        """把市场名折叠为规范名：1X2→HAD，AH→HHAD(-1)。"""
        name = (market or "").strip()
        if name == "1X2":
            return "HAD"
        if name == "AH":
            return "HHAD(-1)"
        return name

    # -- 多玩法（每场比赛的全部玩法） ---------------------------------------

    def list_markets(self, match_id: str) -> Optional[Dict[str, Any]]:
        """列出某场赛事**全部玩法**的去水结果与期望值。

        这是「每场比赛有很多种投注玩法，都要统计计算」的核心入口。
        对每种玩法独立去水（不同玩法的水钱差异极大——实测 HAD 13%
        对 CRS 30%），并给出各自的 EV、方法分歧与告警。

        Returns:
            None 表示赛事不存在；否则含 markets 列表。
        """
        snaps = self._snapshots_of(match_id)
        if not snaps:
            return None

        latest = snaps[-1]
        rows: List[Dict[str, Any]] = []
        seen: set = set()

        for snap in reversed(snaps):
            # 按**规范名**去重：遗留的 1X2 与规范的 HAD 视为同一玩法
            canon = self._canonical_market(snap.market)
            if canon in seen:
                continue
            seen.add(canon)
            rows.append(self._market_row(snap))

        # 按水钱升序：水钱最低者（对玩家最有利）排前面
        rows.sort(key=lambda r: r.get("margin", 1.0))

        margins = [r["margin"] for r in rows if r.get("margin") is not None]
        evs = [r["ev_if_fair"] for r in rows if r.get("ev_if_fair") is not None]
        warned = [r["market"] for r in rows if r.get("spread_warning")]

        return {
            "match_id": match_id,
            "league": latest.league,
            "home": latest.home,
            "away": latest.away,
            "n_markets": len(rows),
            "margins": {
                "min": round(min(margins), 8) if margins else None,
                "max": round(max(margins), 8) if margins else None,
                "spread_pp": (round((max(margins) - min(margins)) * 100, 4)
                              if margins else None),
            },
            "ev_if_fair": {
                "best": round(max(evs), 8) if evs else None,
                "worst": round(min(evs), 8) if evs else None,
            },
            "spread_warning_markets": warned,
            "markets": rows,
        }

    def _market_row(self, snap: OddsSnapshot) -> Dict[str, Any]:
        """单个玩法的去水 + EV + 分歧诊断。"""
        row: Dict[str, Any] = {
            "market": snap.market,
            "n_outcomes": snap.n_outcomes,
            "outcomes": list(snap.outcomes),
            "odds": list(snap.odds),
            "booksum": round(snap.booksum, 8),
            "margin": round(snap.margin, 8),
            "state": snap.state.value,
            "play_name": (snap.metadata or {}).get("玩法名称")
            or (snap.metadata or {}).get("盘口类型"),
        }
        try:
            fp = devig_mod.devig(snap)
        except (ValueError, ZeroDivisionError) as exc:
            row["error"] = "去水失败: %s" % exc
            return row

        row["method"] = fp.method.value
        row["probabilities"] = [round(p, 8) for p in fp.probabilities]
        row["shin_z"] = None if fp.shin_z is None else round(fp.shin_z, 8)
        row["method_spread_pp"] = round(fp.method_spread_pp, 4)
        row["spread_warning"] = fp.spread_warning
        row["ev_if_fair"] = (
            econ.expected_value_from_margin(snap.margin)
            if snap.margin > -1 else None
        )
        # 逐结果的公平赔率：fair_odds_i = 1 / p_i
        row["fair_odds"] = [
            round(1.0 / p, 6) if p > 0 else None for p in fp.probabilities
        ]
        return row

    def get_market(self, match_id: str, market: str) -> Optional[Dict[str, Any]]:
        """单个玩法的详细估值（含逐结果对比）。"""
        snap = self._latest(match_id, market)
        if snap is None:
            return None
        row = self._market_row(snap)
        if "probabilities" in row:
            detail = []
            for i, outcome in enumerate(snap.outcomes):
                p = row["probabilities"][i]
                detail.append({
                    "outcome": outcome,
                    "odds": snap.odds[i],
                    "raw_implied": round(1.0 / snap.odds[i], 8),
                    "fair_prob": p,
                    "fair_odds": row["fair_odds"][i],
                })
            row["results"] = detail
        row["match_id"] = match_id
        row["captured_at"] = snap.captured_at.isoformat()
        row["metadata"] = dict(snap.metadata or {})
        return row

    def market_catalog(self) -> Dict[str, Any]:
        """返回本项目支持的全部玩法目录（供 UI 与接口自描述）。"""
        specs = mk.catalog()
        return {
            "n_markets": len(specs),
            "markets": [sp.as_dict() for sp in specs],
            "note": (
                "体彩官方单场可售 5 种玩法（HAD/HHAD/TTG/CRS/HAFU），"
                "标准赔率源另有 AH/OU/BTTS/DC。"
                "数据缺口：既有 output/场次*.json 仅落库 HAD 与 HHAD 赔率，"
                "其余玩法的赔率需重新采集（采集器已支持，见 "
                "collector.normalizer.parse_pooled_odds）。"
            ),
        }

    def get_model_probs(
        self,
        match_id: str,
        market: str = "HAD",
        shrink: float = 0.3,
        league_avg_goals: float = 2.6,
    ) -> Optional[Dict[str, Any]]:
        """用 Poisson 模型给出某玩法的**模型概率**（独立于市场）。

        与 get_fair() 的根本区别：
          get_fair() 是「市场怎么定价」（去水后的隐含概率）；
          本函数是「模型认为应该怎么定价」（基于球队进球数据）。
          两者之差才是真实的 edge——这正是报告反复强调的：
          没有独立模型，edge 恒为负，任何"优势"都是幻觉。

        数据依赖：详细分析数据.数据统计 里的场均进球/失球。
        缺失时返回 None（**不猜测**）。
        """
        raw = None
        for probe in self._snapshots_of(match_id):
            stats = (probe.metadata or {}).get("进球统计")
            if stats:
                raw = stats
                break
        if raw is None:
            raw = self._goals_stats_of(match_id)
        if raw is None:
            return None

        try:
            lam_h, lam_a = mk.expected_goals_from_stats(
                float(raw["主队场均进球"]), float(raw["主队场均失球"]),
                float(raw["客队场均进球"]), float(raw["客队场均失球"]),
                league_avg_goals, shrink=shrink,
            )
        except (KeyError, TypeError, ValueError) as exc:
            return {"match_id": match_id, "market": market,
                    "error": "进球统计不可用: %s" % exc}

        try:
            spec = mk.spec_by_code(market)
        except KeyError as exc:
            return {"match_id": match_id, "market": market,
                    "error": str(exc)}

        # 对照市场时用**规范名**，否则遗留数据（1X2/AH）会找不到对照
        snap: Optional[OddsSnapshot] = self._latest(match_id, spec.code)

        probs = mk.market_probs(spec, lam_h, lam_a)
        ok, total = mk.partition_check(spec, probs)

        result: Dict[str, Any] = {
            "match_id": match_id,
            "market": spec.code,
            "market_name": spec.name,
            "lambda_home": round(lam_h, 6),
            "lambda_away": round(lam_a, 6),
            "lambda_total": round(lam_h + lam_a, 6),
            "model_probabilities": [round(p, 8) for p in probs],
            "outcomes": list(spec.outcomes),
            "model_fair_odds": [
                round(1.0 / p, 6) if p > 0 else None for p in probs
            ],
            "partition_ok": ok,
            "partition_sum": round(total, 10),
            "shrink": shrink,
            "league_avg_goals": league_avg_goals,
            "model": "independent_poisson",
            "caveats": [
                "独立泊松假设：未建模进球相关性（Dixon-Coles ρ 修正未实现）",
                "未建模红牌/伤停/轮换的实时影响",
                "样本量小（场均数据来自近期 10 场）时偏差较大",
            ],
        }

        # 若同一玩法已有市场赔率，顺便给出模型 vs 市场的对照
        if snap is not None:
            try:
                fp = devig_mod.devig(snap)
                market_p = fp.probabilities
                diffs = [probs[i] - market_p[i] for i in range(len(probs))]
                result["market_probabilities"] = [
                    round(p, 8) for p in market_p]
                result["prob_diff_model_minus_market"] = [
                    round(d, 8) for d in diffs]
                # 逐结果的期望收益（edge）：
                #     EV_i = p_model_i × o_i − 1
                # 这正是报告 §7.1 的判据：只有当模型概率**高于市场定价隐含的
                # 盈亏平衡概率** 1/o_i 时，该腿才有正期望。
                # 注意不能用 (p_model − p_market) × o 去算——那是无意义的量。
                result["model_edge_vs_market"] = [
                    round(probs[i] * snap.odds[i] - 1.0, 8)
                    for i in range(len(probs))
                ]
                result["breakeven_prob"] = [
                    round(1.0 / o, 8) for o in snap.odds
                ]
                result["market_margin"] = round(snap.margin, 8)
            except (ValueError, ZeroDivisionError) as exc:
                result["market_error"] = str(exc)

        return result

    def _goals_stats_of(self, match_id: str) -> Optional[Dict[str, float]]:
        """从语料里回捞该场的场均进球/失球（若语料可用）。

        既有 output JSON 的「详细分析数据.数据统计」并非快照的一部分，
        因此这里的回捞是**尽力而为**：找不到就返回 None，绝不编造。
        """
        if not self.corpus_root:
            return None
        for rec in load_corpus(self.corpus_root):
            basic = rec.get("基本信息") or {}
            mid = str(basic.get("场次号") or basic.get("比赛ID") or "")
            if mid != str(match_id):
                continue
            stats = ((rec.get("详细分析数据") or {}).get("数据统计") or {})
            try:
                return {
                    "主队场均进球": float(
                        (stats.get("进球平均数") or {})["主场平均进球"]),
                    "主队场均失球": float(
                        (stats.get("失球平均数") or {})["主场平均失球"]),
                    "客队场均进球": float(
                        (stats.get("进球平均数") or {})["客场平均进球"]),
                    "客队场均失球": float(
                        (stats.get("失球平均数") or {})["客场平均失球"]),
                }
            except (KeyError, TypeError, ValueError):
                return None
        return None

    def cross_market_check(
        self, match_id: str, tol: float = 0.02,
    ) -> Optional[Dict[str, Any]]:
        """跨玩法边际一致性校验（报告 §8.4 套利与数据错误检测）。"""
        snaps = self._snapshots_of(match_id)
        if not snaps:
            return None

        per_market: Dict[str, List[float]] = {}
        seen: set = set()
        for snap in reversed(snaps):
            if snap.market in seen:
                continue
            seen.add(snap.market)
            try:
                fp = devig_mod.devig(snap)
            except (ValueError, ZeroDivisionError):
                continue
            per_market[self._canonical_market(snap.market)] = \
                list(fp.probabilities)

        findings = mk.cross_market_marginals(per_market, tol=tol)
        return {
            "match_id": match_id,
            "n_markets_checked": len(per_market),
            "tolerance": tol,
            "n_inconsistencies": len(findings),
            "inconsistencies": findings,
            "interpretation": (
                "不一致可能源于：a) 采集/解析错误（更常见）；"
                "b) 不同玩法间的真实套利（报告 §8.4）。"
                "两者都需要人工介入，本接口不做自动裁定。"
            ) if findings else "各玩法边际自洽",
        }

    # -- 估值 ---------------------------------------------------------------

    def get_fair(self, match_id: str, market: str = "1X2",
                 method: str = "auto",
                 fallback: bool = True) -> Optional[Dict[str, Any]]:
        """去水后的公平概率。

        fallback=True 时，若该场没有请求的市场（乐鱼源常无胜平负），
        降级到任意可用市场，而不是直接返回空。
        """
        snap = self._latest(match_id, market, fallback=fallback)
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
        # 必须回传**实际**使用的市场：请求 1X2 而被降级到 AH/OU 时，
        # 前端若不知情就会把两结果市场当成胜平负展示，产生误导。
        d["market"] = snap.market
        d["requested_market"] = market
        d["market_fallback"] = self._canonical_market(
            snap.market) != self._canonical_market(market)
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
