#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
乐鱼（leyu）源归一化器 —— 把 leyu 报价映射为本项目的领域快照。

分层职责（AGENTS.md §3.2）：
    leyu_client（HTTP + 原始解析）→ **本模块**（语义映射）→ OddsSnapshot → 下游分析

## 市场映射（全部来自抓包实测字段）

乐鱼以 `chpid` 标识盘口，`hpt` 标识类型：

| leyu chpid | hpt | 含义 | 本项目 market |
| --- | --- | --- | --- |
| 1   | 1 | 全场独赢       | `HAD` |
| 17  | 1 | 上半场独赢     | `HAD_1H`（自有玩法，不参与跨市场边际校验） |
| 4   | 2 | 全场让球       | `AH(<hv>)` —— **两向，无平局** |
| 19  | 2 | 上半场让球     | `AH_1H(<hv>)` —— 两向，无平局 |
| 2   | 5 | 全场大小       | `OU(<hv>)` |
| 18  | 5 | 上半场大小     | `OU_1H(<hv>)` |

> ⚠️ **绝不能把乐鱼让球盘映射为 `HHAD`**：`HHAD` 是体彩的**三向**让球胜平负
> （home/draw/away），而乐鱼 `chpid=4/19` 实测每个盘口线只有 **2 个选项**
> （`1`=主队让球方、`2`=客队让球方，无平局）。
> 早期版本误映射为 `HHAD` 会导致每个盘口都因「缺少结果 draw」而被丢弃，
> 并需按主队方向翻转线值——现已用 `AH` 直接透传 `hv`。

`hv` 语义经实测确认：**`hv` 是主队让球线（有符号）**，与 `core.markets.AH/OU`
的 `line` 约定一致，可直接透传。证据：同一区块内两条线的 `hv` 与选项标签严格
互补（`hv="0.5"` → `1:+0.5 / 2:-0.5`；`hv="-0.5"` → `1:-0.5 / 2:+0.5`）。

## 状态映射（报告 §5.3「停盘价格不得进入 edge 计算」）

| 条件 | SnapshotState |
| --- | --- |
| `ms == 110`（已结束）或盘口缺失 | `DELISTED` |
| 报价(`ctsp`)早于采集时刻超过 `stale_after` | `STALE` |
| 其余 | `ACTIVE` |

乐鱼的盘口快照**没有**显式的「停盘」标志位（`hpsPns[].hshow` 为是否展示，
不能当作停盘）。因此本层不臆造 SUSPENDED——停盘只能通过下一节所述
的**推送事件**（`C104` 盘口状态）得知，在线源留给上层处理。

## 保真原则

- 原始标识（`chpid`/`chpid` 名称/数据源 `cds`/马来水位 `ov2`）全部写入
  `OddsSnapshot.metadata`，便于溯源与跨源比对。
- 只丢弃**无法解释**的盘口（记告警），绝不静默改写赔率。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from core.markets import AH, HAD, MarketSpec, OU
from core.models import OddsSnapshot, SnapshotState, utcnow

from .leyu_client import LEYUMatch, MarketQuote, OddsQuote

__all__ = [
    "DEFAULT_SOURCE",
    "MARKET_CHPID_1X2",
    "MARKET_CHPID_1H_1X2",
    "MARKET_CHPID_AH",
    "MARKET_CHPID_1H_AH",
    "MARKET_CHPID_OU",
    "MARKET_CHPID_1H_OU",
    "DEFAULT_STALE_AFTER",
    "spec_for_market_quote",
    "snapshots_from_market",
    "snapshots_from_match",
    "normalize_leyu_matches",
]

#: 数据来源标识（贯穿 L1→L6，用于审计与跨源区分）
DEFAULT_SOURCE = "乐鱼API"

# --------------------------------------------------------------------------- #
# chpid 常量（与 leyu_client 的 HPT_* 配合使用）
# --------------------------------------------------------------------------- #

MARKET_CHPID_1X2 = "1"      # 全场独赢
MARKET_CHPID_1H_1X2 = "17"  # 上半场独赢
MARKET_CHPID_AH = "4"       # 全场让球
MARKET_CHPID_1H_AH = "19"   # 上半场让球
MARKET_CHPID_OU = "2"       # 全场大小
MARKET_CHPID_1H_OU = "18"   # 上半场大小

#: 默认陈旧阈值：报价超过该时长未见更新即视为 STALE（秒）
#: 依据：WebSocket 正常推送下赔率秒级刷新，300s 未动可判定链路或该盘口已冻结。
DEFAULT_STALE_AFTER = 300.0

# --------------------------------------------------------------------------- #
# 线值解析
# --------------------------------------------------------------------------- #

#: 亚盘复合线，如 "1/1.5"、"0/0.5"、"-0.5/1"
def parse_hv(hv: object) -> Optional[float]:
    """把 leyu 的 `hv` 线值解析为 float。

    支持普通线（"1.5"、"-1"）与复合线（"1/1.5"、"-0.5/1"，取中点）。
    独赢盘 `hv` 为空串，返回 None（调用方按无盘口处理）。
    """
    if hv is None:
        return None
    text = str(hv).strip()
    if not text:
        return None
    if "/" in text:
        parts = [p.strip() for p in text.split("/") if p.strip()]
        vals: List[float] = []
        for p in parts:
            try:
                vals.append(float(p))
            except ValueError:
                return None
        if not vals:
            return None
        return sum(vals) / len(vals)
    # "3+" / "7+" 这类聚合桶不是连续让球线，无法参与线值计算
    if text.endswith("+"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def spec_for_market_quote(mq: MarketQuote) -> Optional[MarketSpec]:
    """把乐鱼盘口映射为本项目的 MarketSpec；无法映射返回 None。

    **让球族用两向 `AH`，不是三向 `HHAD`**（实测 leyu `chpid=4/19` 只有 2 个选项）。
    上半场玩法本项目原目录没有对应项（体彩源不含半场），这里给出稳定的
    自有 code，不参与 `cross_market_marginals` 校验，但仍可独立去水/算 EV。
    """
    line = parse_hv(mq.hv)
    if mq.chpid == MARKET_CHPID_1X2:
        return HAD
    if mq.chpid == MARKET_CHPID_AH:
        return AH(0.0 if line is None else line)
    if mq.chpid == MARKET_CHPID_OU:
        if line is None:
            return None
        return OU(line)
    if mq.chpid == MARKET_CHPID_1H_1X2:
        return _extra_spec("HAD_1H", "上半场胜平负", HAD.outcomes)
    if mq.chpid == MARKET_CHPID_1H_AH:
        return _extra_spec("AH_1H(%s)" % _fmt(line), "上半场让球",
                           ("home", "away"), line=line)
    if mq.chpid == MARKET_CHPID_1H_OU:
        if line is None:
            return None
        return _extra_spec("OU_1H(%s)" % _fmt(line), "上半场大小球",
                           ("over", "under"), line=line)
    return None


def _fmt(line: Optional[float]) -> str:
    if line is None:
        return "?"
    return ("%g" % line)


def _extra_spec(code: str, name: str, outcomes: Sequence[str],
                line: Optional[float] = None) -> MarketSpec:
    """构造乐鱼自有玩法的 MarketSpec（非体彩 poolCode）。"""
    return MarketSpec(
        code=code,
        name=name,
        outcomes=tuple(outcomes),
        line=line,
        notes="乐鱼自有玩法（非体彩目录）",
    )


# --------------------------------------------------------------------------- #
# 结果对齐
# --------------------------------------------------------------------------- #

def _ordered_odds(
    mq: MarketQuote, spec: MarketSpec
) -> Tuple[Optional[Tuple[float, ...]], List[str]]:
    """按 `spec.outcomes` 顺序排列赔率；缺项返回 (None, 缺失结果名列表)。

    不用 `OddsSnapshot` 的构造异常兜底，是为了给出**可读的**缺失原因。
    """
    by_outcome: Dict[str, float] = {}
    for q in mq.quotes:
        # 同名结果只保留第一个（重复即上游异常，交由告警体现）
        by_outcome.setdefault(q.outcome, q.decimal)
    missing: List[str] = [o for o in spec.outcomes if o not in by_outcome]
    if missing:
        return None, missing
    return tuple(by_outcome[o] for o in spec.outcomes), []


def _state_for(match: LEYUMatch, mq: MarketQuote, captured: datetime,
               stale_after: float) -> SnapshotState:
    """推导快照状态（见模块文档的状态映射表）。"""
    if match.is_finished:
        return SnapshotState.DELISTED
    if mq.ctsp > 0:
        age = captured - datetime.fromtimestamp(mq.ctsp / 1000.0, tz=timezone.utc)
        # 上游时钟可能略快于本机，容差 0 但不把未来时间当成陈旧
        if age > timedelta(seconds=stale_after):
            return SnapshotState.STALE
    return SnapshotState.ACTIVE


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def snapshots_from_market(
    match: LEYUMatch,
    mq: MarketQuote,
    captured: Optional[datetime] = None,
    stale_after: float = DEFAULT_STALE_AFTER,
    source: str = DEFAULT_SOURCE,
    issues: Optional[List[str]] = None,
) -> List[OddsSnapshot]:
    """单个盘口 → 0..1 个 OddsSnapshot。

    Args:
        match: 赛事基础信息（提供联赛/队名/状态）。
        mq: 盘口报价。
        captured: 采集时刻（UTC）；默认当前时间。
        stale_after: 陈旧阈值（秒）。
        source: 数据来源标识。
        issues: 告警收集列表（字符串，便于上层聚合）。

    Returns:
        快照列表（映射失败或结果不全时为空）。
    """
    sink = issues if issues is not None else []
    spec = spec_for_market_quote(mq)
    if spec is None:
        sink.append("赛事 %s：无法映射盘口 chpid=%s(%s line=%r)，已跳过"
                    % (match.mid, mq.chpid, mq.name, mq.hv))
        return []

    odds, missing = _ordered_odds(mq, spec)
    if odds is None:
        sink.append("赛事 %s 盘口 %s：缺少结果 %s，已跳过"
                    % (match.mid, spec.code, ",".join(missing)))
        return []

    when = captured or utcnow()
    meta: Dict[str, Any] = {
        "数据来源": source,
        # 原始标识，便于溯源与跨源比对（AGENTS.md §5.3 证据可核验）
        "leyu_chpid": mq.chpid,
        "leyu_盘口名": mq.name,
        "leyu_hv": mq.hv,
        "leyu_hpt": mq.hpt,
        "leyu_ctsp": mq.ctsp,
        "leyu_cds": sorted({q.source for q in mq.quotes if q.source}),
        "leyu_ov2": {q.outcome: q.malay for q in mq.quotes if q.malay},
        "联赛": match.tournament,
        "赛事状态": match.status,
    }
    if spec.code not in (HAD.code,):
        meta["盘口线"] = spec.line
    try:
        return [OddsSnapshot(
            match_id=match.mid,
            league=match.tournament,
            home=match.home,
            away=match.away,
            market=spec.code,
            outcomes=spec.outcomes,
            odds=odds,
            state=_state_for(match, mq, when, stale_after),
            captured_at=when,
            source=source,
            metadata=meta,
        )]
    except (ValueError, TypeError) as exc:
        sink.append("赛事 %s 盘口 %s：构造快照失败 %s" % (match.mid, spec.code, exc))
        return []


def snapshots_from_match(
    match: LEYUMatch,
    captured: Optional[datetime] = None,
    stale_after: float = DEFAULT_STALE_AFTER,
    source: str = DEFAULT_SOURCE,
    issues: Optional[List[str]] = None,
) -> List[OddsSnapshot]:
    """一场赛事的全部盘口 → 快照列表（主盘口 + 附加盘口线全部覆盖）。"""
    out: List[OddsSnapshot] = []
    for mq in match.markets:
        out.extend(snapshots_from_market(match, mq, captured, stale_after,
                                         source, issues))
    return out


def normalize_leyu_matches(
    matches: Iterable[LEYUMatch],
    captured: Optional[datetime] = None,
    stale_after: float = DEFAULT_STALE_AFTER,
    source: str = DEFAULT_SOURCE,
) -> Tuple[List[OddsSnapshot], List[str]]:
    """批量归一化。

    Returns:
        `(snapshots, issues)`；issues 为可读告警列表（不中断整批）。
    """
    snaps: List[OddsSnapshot] = []
    issues: List[str] = []
    for m in matches:
        snaps.extend(snapshots_from_match(m, captured, stale_after, source, issues))
    return snaps, issues


def booksum_of(mq: MarketQuote) -> float:
    """盘口水位 Σ(1/赔率)，供上层做合理性抽检。"""
    return math.fsum(q.implied for q in mq.quotes)
