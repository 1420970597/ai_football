#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
赛事归一化 —— 把上游原始 JSON 映射为领域模型

对应报告：
  §3.2  多路径采集的**汇聚点**：无论来源是官方 API、逆向 GraphQL 还是浏览器
        渲染，最终都要归一化到同一套 OddsSnapshot，否则下游无法统一处理。
  §3.3  协议漂移是常态：上游字段名会变，因此解析器必须**容忍缺字段**，
        并把缺失情况显式记录，而不是抛异常中断整批采集。
  §4.4  完整性六维：本模块负责其中「行级完整（无缺失字段）」与
        「幂等去重（跨源对齐）」两维。

上游数据来源：中国体育彩票官方 API V2（output/场次*.json）
真实结构（见仓库 output/ 样例）：
    {
      "基本信息": {"场次号":2001, "联赛简称":"日职", "主队名称":..., ...},
      "赔率信息": {"主胜赔率":"2.13", "平局赔率":"3.45", "客胜赔率":"2.70",
                   "让球主胜赔率":..., "让球盘口":"-1.00"},
      "玩法信息": {"可用玩法":[{"玩法代码":"HHAD","玩法状态":"Selling"},...]},
      "数据获取信息": {"获取时间":"2025-09-23T13:42:18", "数据来源":"..."}
    }

多玩法支持（本次扩展）：

体彩官方单场可售 5 种玩法（HAD/HHAD/TTG/CRS/HAFU），但原始
output/场次*.json 只保存了 HAD 与 HHAD 的赔率——其余玩法虽标记
为 Selling，赔率并未落库。本模块提供两条路径：

  A) **legacy**：解析既有 output JSON（仅 1X2 + 让球），保持向后兼容。
  B) **oddsList**：解析官方接口的 oddsList 条目，**覆盖全部玩法**。
     键名映射集中在 core.markets（HAD_ODDS_KEYS / TTG_ODDS_KEYS /
     HAFU_ODDS_KEYS / parse_crs_key）。

未知按键一律记录 ParseIssue 并跳过该玩法，**绝不静默丢弃**：
静默丢弃会把「数据缺失」伪装成「水钱很低」，从而产生错误的
套利结论（报告 §4.4 完整性要求）。

依赖：仅标准库。
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from core import markets as mk
from core.models import OddsSnapshot, SnapshotState, utcnow

__all__ = [
    "parse_match",
    "normalize_matches",
    "MapResult",
    "ParseIssue",
    "MARKET_1X2",
    "MARKET_HANDICAP",
    "OUTCOMES_1X2",
    "OUTCOMES_HANDICAP",
    "parse_odds_list",
    "parse_pooled_odds",
]

MARKET_1X2 = "1X2"
MARKET_HANDICAP = "AH"

#: 官方 poolCode 字段名（用于从 oddsList 条目里判断玩法）
_POOL_CODE_FIELD = "poolCode"
OUTCOMES_1X2: Tuple[str, str, str] = ("home", "draw", "away")
# 让球盘的结果命名同样用 home/draw/away，盘口值记录在 metadata
OUTCOMES_HANDICAP: Tuple[str, str, str] = ("home", "draw", "away")

# 玩法代码 → 状态映射（上游用 Selling/Suspended 等）
_STATE_MAP = {
    "selling": SnapshotState.ACTIVE,
    "open": SnapshotState.ACTIVE,
    "active": SnapshotState.ACTIVE,
    "suspended": SnapshotState.SUSPENDED,
    "suspend": SnapshotState.SUSPENDED,
    "closed": SnapshotState.DELISTED,
    "delisted": SnapshotState.DELISTED,
    "ended": SnapshotState.DELISTED,
}


class ParseIssue:
    """单条记录的解析告警（不中断整批处理）。"""

    __slots__ = ("match_id", "field", "detail")

    def __init__(self, match_id: str, field: str, detail: str) -> None:
        self.match_id = match_id
        self.field = field
        self.detail = detail

    def as_dict(self) -> Dict[str, str]:
        return {"match_id": self.match_id, "field": self.field,
                "detail": self.detail}

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "ParseIssue(%r, %r, %r)" % (self.match_id, self.field, self.detail)


class MapResult:
    """归一化结果：快照 + 告警 + 统计。"""

    __slots__ = ("snapshots", "issues", "n_matches", "n_skipped")

    def __init__(self) -> None:
        self.snapshots: List[OddsSnapshot] = []
        self.issues: List[ParseIssue] = []
        self.n_matches = 0
        self.n_skipped = 0

    def add_issue(self, match_id: str, field: str, detail: str) -> None:
        self.issues.append(ParseIssue(match_id, field, detail))

    def summary(self) -> Dict[str, Any]:
        return {
            "n_matches": self.n_matches,
            "n_snapshots": len(self.snapshots),
            "n_issues": len(self.issues),
            "n_skipped": self.n_skipped,
            "issues": [i.as_dict() for i in self.issues[:50]],
        }


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def _finite(v: object, name: str, issues: Optional[List[ParseIssue]] = None,
            match_id: str = "") -> Optional[float]:
    """安全转 float；失败时记录告警并返回 None（容忍脏数据）。"""
    try:
        out = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        if issues is not None:
            issues.append(ParseIssue(match_id, name, "无法转为数值: %r" % (v,)))
        return None
    if not math.isfinite(out):
        if issues is not None:
            issues.append(ParseIssue(match_id, name, "非有限值: %r" % (v,)))
        return None
    return out


def _clean_str(v: object) -> str:
    if v is None:
        return ""
    return str(v).strip()


_TIME_RE = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{1,2})")


def _parse_capture_time(raw: object, fallback: datetime) -> datetime:
    """解析「获取时间」字段（形如 2025-09-23T13:42:18.843785）。

    无时区信息时按 UTC 处理（上游为国内数据，但快照语义统一用 UTC 存储，
    展示层再转本地时区）。
    """
    if not raw:
        return fallback
    s = _clean_str(raw)
    # 统一分隔符
    s = s.replace("/", "-").replace(" ", "T", 1) if "T" not in s else s
    try:
        ts = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return fallback
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _match_state(match: Mapping[str, Any], odds_block: Mapping[str, Any],
                 play_block: Mapping[str, Any]) -> SnapshotState:
    """推断比赛状态。

    优先级（后者在前者为 ACTIVE 时才生效）：
        1) 销售状态 / 比赛状态的**硬门禁**（停售、已结束）
        2) 玩法级状态（最贴近报价可用性）
        3) 默认 ACTIVE

    为何销售状态优先：`销售状态=0` 意味着整个赛事停售，属于**全局硬门禁**；
    此时即便某个玩法仍标记 Selling，该快照也不可用于信号
    （报告 §5.3：非 Selling 状态意味着报价被挂起或撤下）。
    """
    # 1) 硬门禁：赛事级状态优先级最高
    for key in ("销售状态", "比赛状态"):
        raw = _clean_str((match or {}).get(key)).lower()
        if not raw:
            continue
        if raw in _STATE_MAP and _STATE_MAP[raw] is not SnapshotState.ACTIVE:
            return _STATE_MAP[raw]
        # 销售状态为 "0"/"false" 表示停售（上游用 0/1 表示）
        if key == "销售状态" and raw in ("0", "false", "closed", "end"):
            return SnapshotState.DELISTED

    # 2) 玩法级状态：仅在有非 selling 时降级
    plays = (play_block or {}).get("可用玩法") or []
    if isinstance(plays, (list, tuple)) and plays:
        states = set()
        for p in plays:
            if isinstance(p, Mapping):
                st = _clean_str(p.get("玩法状态")).lower()
                if st:
                    states.add(st)
        if states and not all(s in ("selling", "open", "active") for s in states):
            for s in states:
                if s in _STATE_MAP and _STATE_MAP[s] is not SnapshotState.ACTIVE:
                    return _STATE_MAP[s]

    return SnapshotState.ACTIVE


# --------------------------------------------------------------------------- #
# 单场解析
# --------------------------------------------------------------------------- #

def parse_match(raw: Mapping[str, Any],
                source: str = "体彩官方API",
                default_time: Optional[datetime] = None,
                issues: Optional[List[ParseIssue]] = None) -> List[OddsSnapshot]:
    """把单场原始记录解析为 0..N 个快照（1X2 + 让球，各自可能有状态）。

    Args:
        raw: 单场原始字典。
        source: 数据来源标识。
        default_time: 无法解析「获取时间」时的回退时间。
        issues: 告警收集列表（可选，便于批量调用方聚合）。

    Returns:
        快照列表。缺失关键字段时返回空列表并写入告警。
    """
    issue_sink = issues if issues is not None else []
    fallback = default_time or utcnow()

    basic = raw.get("基本信息") or {}
    odds_block = raw.get("赔率信息") or {}
    play_block = raw.get("玩法信息") or {}
    meta_block = raw.get("数据获取信息") or {}

    if not isinstance(basic, Mapping):
        issue_sink.append(ParseIssue("?", "基本信息", "不是字典"))
        return []

    match_id = _clean_str(basic.get("场次号")) or _clean_str(basic.get("比赛ID"))
    if not match_id:
        issue_sink.append(ParseIssue("?", "场次号", "缺失，无法建立赛事标识"))
        return []

    league = _clean_str(basic.get("联赛简称")) or _clean_str(basic.get("联赛名称"))
    home = _clean_str(basic.get("主队名称")) or _clean_str(basic.get("主队全称"))
    away = _clean_str(basic.get("客队名称")) or _clean_str(basic.get("客队全称"))
    if not (home and away):
        issue_sink.append(ParseIssue(match_id, "队名", "主/客队名缺失"))
        return []

    captured = _parse_capture_time(meta_block.get("获取时间"), fallback)
    state = _match_state(basic, odds_block, play_block)

    base_meta: Dict[str, Any] = {
        "场次编号字符串": _clean_str(basic.get("场次编号字符串")),
        "比赛日期": _clean_str(basic.get("比赛日期")),
        "比赛时间": _clean_str(basic.get("比赛时间")),
        "联赛名称": _clean_str(basic.get("联赛名称")),
        "比赛ID": _clean_str(basic.get("比赛ID")),
        "数据来源": _clean_str(meta_block.get("数据来源")) or source,
        "接口版本": _clean_str(meta_block.get("接口版本")),
    }

    out: List[OddsSnapshot] = []

    # -- 1X2 ---------------------------------------------------------------
    o = [
        _finite(odds_block.get("主胜赔率"), "主胜赔率", issue_sink, match_id),
        _finite(odds_block.get("平局赔率"), "平局赔率", issue_sink, match_id),
        _finite(odds_block.get("客胜赔率"), "客胜赔率", issue_sink, match_id),
    ]
    out.extend(_build(match_id, league, home, away, MARKET_1X2, OUTCOMES_1X2,
                      o, base_meta, state, captured, source, issue_sink,
                      extra={"盘口类型": "胜平负"}))

    # -- 让球（亚盘） -------------------------------------------------------
    h = [
        _finite(odds_block.get("让球主胜赔率"), "让球主胜赔率", issue_sink, match_id),
        _finite(odds_block.get("让球平局赔率"), "让球平局赔率", issue_sink, match_id),
        _finite(odds_block.get("让球客胜赔率"), "让球客胜赔率", issue_sink, match_id),
    ]
    handicap = _finite(odds_block.get("让球盘口"), "让球盘口", issue_sink, match_id)
    out.extend(_build(match_id, league, home, away, MARKET_HANDICAP,
                      OUTCOMES_HANDICAP, h, base_meta, state, captured, source,
                      issue_sink,
                      extra={"盘口类型": "让球胜平负",
                             "让球盘口": handicap}))

    if not out:
        issue_sink.append(ParseIssue(match_id, "赔率", "1X2 与让球均无有效赔率"))
    return out


def _build(match_id: str, league: str, home: str, away: str,
           market: str, outcomes: Sequence[str], odds: Sequence[Optional[float]],
           base_meta: Mapping[str, Any], state: SnapshotState,
           captured: datetime, source: str, issues: List[ParseIssue],
           extra: Optional[Mapping[str, Any]] = None) -> List[OddsSnapshot]:
    """构造一个市场的快照；赔率不完整时跳过并告警。"""
    if any(v is None for v in odds):
        missing = sum(1 for v in odds if v is None)
        issues.append(ParseIssue(
            match_id, market, "赔率不完整（缺 %d/%d），已跳过该市场"
            % (missing, len(odds))))
        return []
    meta = dict(base_meta)
    if extra:
        meta.update(extra)
    try:
        return [OddsSnapshot(
            match_id=match_id, league=league, home=home, away=away,
            market=market, outcomes=tuple(outcomes),
            odds=tuple(float(v) for v in odds),  # type: ignore[arg-type]
            state=state, captured_at=captured, source=source, metadata=meta,
        )]
    except (ValueError, TypeError) as exc:
        issues.append(ParseIssue(match_id, market, "构造快照失败: %s" % exc))
        return []


# --------------------------------------------------------------------------- #
# 多玩法：从官方 oddsList 解析全部玩法
# --------------------------------------------------------------------------- #

#: 稳态的 poolCode → 市场规格构造（盘口由条目自身给出）
def _spec_for_pool(pool_code: str,
                   goal_line: Optional[float]) -> Optional[mk.MarketSpec]:
    """poolCode + 盘口 → 市场规格；无法识别返回 None。"""
    code = pool_code.strip().upper()
    if code == "HAD":
        return mk.HAD
    if code == "HHAD":
        # 体彩让球盘口以主队让球数给出（"-1.00" 表示主队让 1 球）
        return mk.HHAD(-1.0 if goal_line is None else goal_line)
    if code == "TTG":
        return mk.TTG
    if code == "CRS":
        return mk.CRS
    if code == "HAFU":
        return mk.HAFU
    if code == "OU":
        if goal_line is None:
            return mk.OU(2.5)
        return mk.OU(goal_line)
    if code == "BTTS":
        return mk.BTTS
    if code == "DC":
        return mk.DC
    if code == "AH":
        return mk.AH(-0.5 if goal_line is None else goal_line)
    return None


def _odds_from_entry(entry: Mapping[str, Any],
                     spec: mk.MarketSpec,
                     match_id: str,
                     issues: List[ParseIssue]) -> Optional[List[Optional[float]]]:
    """按 spec.outcomes 顺序从 oddsList 条目里取出赔率。

    未知键记录告警；缺失的结果位留 None 交由 _build 统一处理。
    """
    pool = _clean_str(entry.get(_POOL_CODE_FIELD))
    keys = mk.pool_odds_keys(pool) if pool else None

    found: Dict[str, float] = {}

    for raw_key, raw_val in entry.items():
        if not isinstance(raw_key, str):
            continue
        key = raw_key.strip()
        if key in ("poolCode", "goalLine", "updateTime", "matchId",
                   "singleFlag", "passFlag", "id"):
            continue

        outcome: Optional[str] = None
        if pool and pool.strip().upper() == "CRS":
            outcome = mk.parse_crs_key(key)
        elif keys is not None:
            outcome = keys.get(key) or keys.get(key.lower())
        else:
            # 无映射表的玩法（未知 poolCode）→ 记录一次告警
            issues.append(ParseIssue(
                match_id, pool or "未知玩法",
                "无键名映射，无法解析字段 %r" % key))
            continue

        if outcome is None:
            issues.append(ParseIssue(
                match_id, pool or spec.code,
                "无法识别的赔率键 %r（已忽略；若是编码变更请更新 "
                "core.markets 的键映射）" % key))
            continue

        val = _finite(raw_val, "oddsList.%s" % key, issues, match_id)
        if val is not None:
            found[outcome] = val

    if not found:
        return None

    return [found.get(o) for o in spec.outcomes]


def parse_odds_list(
    entry: Mapping[str, Any],
    match_id: str,
    league: str,
    home: str,
    away: str,
    captured: datetime,
    base_meta: Mapping[str, Any],
    state: SnapshotState,
    source: str,
    issues: List[ParseIssue],
) -> List[OddsSnapshot]:
    """解析**单条** oddsList 条目 → 0..1 个快照。

    与 parse_match() 的区别：parse_match 面向既有的 output JSON
    （只有 1X2 + 让球两个字段块）；本函数面向官方接口的 oddsList，
    可覆盖 HAD/HHAD/TTG/CRS/HAFU/OU/BTTS/DC/AH 全部玩法。
    """
    if not isinstance(entry, Mapping):
        issues.append(ParseIssue(match_id, "oddsList", "条目不是字典"))
        return []

    pool = _clean_str(entry.get(_POOL_CODE_FIELD))
    if not pool:
        issues.append(ParseIssue(match_id, "oddsList", "缺少 poolCode"))
        return []

    # goalLine 是**可选**字段：只有让球/大小球类玩法才有。
    # 缺失时不告警（否则每场都会刷 4 条噪音），仅当存在却非法时才记录。
    raw_line = entry.get("goalLine")
    goal_line: Optional[float] = None
    if raw_line is not None and _clean_str(raw_line) != "":
        goal_line = _finite(raw_line, "goalLine", issues, match_id)
    spec = _spec_for_pool(pool, goal_line)
    if spec is None:
        issues.append(ParseIssue(
            match_id, pool, "未支持的玩法代码（已跳过）"))
        return []

    odds = _odds_from_entry(entry, spec, match_id, issues)
    if odds is None:
        issues.append(ParseIssue(
            match_id, spec.code, "该玩法未解析出任何有效赔率（已跳过）"))
        return []

    extra: Dict[str, Any] = {"玩法代码": pool, "玩法名称": spec.name}
    if spec.line is not None:
        extra["盘口"] = spec.line

    market = spec.code if spec.line is None else spec.code
    return _build(match_id, league, home, away, market, spec.outcomes,
                  odds, base_meta, state, captured, source, issues,
                  extra=extra)


def parse_pooled_odds(
    raw: Mapping[str, Any],
    source: str = "体彩官方API",
    default_time: Optional[datetime] = None,
    issues: Optional[List[ParseIssue]] = None,
) -> List[OddsSnapshot]:
    """从**官方接口原始结构**解析单场的全部玩法赔率。

    期望结构（官方 getMatchListV1 / getMatchCalculatorV1 的 subMatchList 元素）::

        {
          "matchNum": 2001, "leagueAbbName": "日职",
          "homeTeamAbbName": "...", "awayTeamAbbName": "...",
          "matchStatus": "Selling",
          "oddsList": [
            {"poolCode": "HAD",  "h": "2.13", "d": "3.45", "a": "2.70"},
            {"poolCode": "HHAD", "h": "4.50", "d": "3.75", "a": "1.56",
             "goalLine": "-1.00"},
            {"poolCode": "TTG",  "s0": "9.0", "s1": "4.6", ...},
            ...
          ]
        }
    """
    issue_sink = issues if issues is not None else []
    fallback = default_time or utcnow()

    if not isinstance(raw, Mapping):
        issue_sink.append(ParseIssue("?", "记录", "不是字典"))
        return []

    match_id = (_clean_str(raw.get("matchNum"))
                or _clean_str(raw.get("matchId")))
    if not match_id:
        issue_sink.append(ParseIssue("?", "场次号", "缺失，无法建立赛事标识"))
        return []

    league = (_clean_str(raw.get("leagueAbbName"))
              or _clean_str(raw.get("leagueAllName")))
    home = (_clean_str(raw.get("homeTeamAbbName"))
            or _clean_str(raw.get("homeTeamAllName")))
    away = (_clean_str(raw.get("awayTeamAbbName"))
            or _clean_str(raw.get("awayTeamAllName")))
    if not (home and away):
        issue_sink.append(ParseIssue(match_id, "队名", "主/客队名缺失"))
        return []

    captured = _parse_capture_time(
        raw.get("updateTime") or raw.get("businessDate"), fallback)
    state = _match_state(raw, {}, {})

    base_meta: Dict[str, Any] = {
        "联赛名称": _clean_str(raw.get("leagueAllName")),
        "比赛日期": _clean_str(raw.get("matchDate")),
        "比赛时间": _clean_str(raw.get("matchTime")),
        "比赛ID": _clean_str(raw.get("matchId")),
        "数据来源": source,
    }

    odds_list = raw.get("oddsList") or []
    if not isinstance(odds_list, (list, tuple)):
        issue_sink.append(ParseIssue(match_id, "oddsList", "不是列表"))
        return []

    if not odds_list:
        issue_sink.append(ParseIssue(
            match_id, "oddsList", "为空（该场可能尚未开盘）"))

    out: List[OddsSnapshot] = []
    for entry in odds_list:
        out.extend(parse_odds_list(entry, match_id, league, home, away,
                                   captured, base_meta, state, source,
                                   issue_sink))
    return out


# --------------------------------------------------------------------------- #
# 批量归一化
# --------------------------------------------------------------------------- #

def normalize_matches(
    records: Sequence[Mapping[str, Any]],
    source: str = "体彩官方API",
    default_time: Optional[datetime] = None,
    dedupe: bool = True,
) -> MapResult:
    """批量归一化，并可选做**幂等去重**（报告 §4.4 完整性维度之一）。

    去重键：(match_id, market, captured_at, odds)。同一份原始数据
    被重复投喂时不会产生重复快照。

    Args:
        records: 原始记录序列。
        source: 数据来源标识。
        default_time: 时间回退值。
        dedupe: 是否去重。

    Returns:
        MapResult（含 snapshots / issues / 统计）。
    """
    res = MapResult()
    seen: set = set()

    for rec in records:
        if not isinstance(rec, Mapping):
            res.n_skipped += 1
            res.add_issue("?", "记录", "不是字典，已跳过")
            continue
        res.n_matches += 1
        snaps = parse_match(rec, source=source, default_time=default_time,
                            issues=res.issues)
        if not snaps:
            res.n_skipped += 1
            continue
        for s in snaps:
            if dedupe:
                key = (s.match_id, s.market,
                       s.captured_at.isoformat(), s.odds)
                if key in seen:
                    continue
                seen.add(key)
            res.snapshots.append(s)

    return res
