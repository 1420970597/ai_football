#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盘口 → 人类可读标签（用户要求：最终结果要说「上半场大1.5」这种话）。

## 为什么单独成模块

原始盘口代码是**机器格式**（`OU_1H(1.5)` + `over`），对人不友好：

    OU_1H(1.5) / over   →  应为「上半场大1.5」
    AH_1H(0.25) / home  →  应为「上半场主队让0/0.5」
    HAD_1H / home       →  应为「上半场主胜（独赢）」

决策结论是给人看的（要据此下注/复核），必须说人话。
本模块把 `(market, outcome)` 翻译成中文，供 API 与前端统一使用。

## 盘口代码来源（实测，见 docs/architecture/leyu-api-protocol.md）

| 代码 | 含义 | 线值来源 |
| --- | --- | --- |
| `HAD` / `HAD_1H` | 全场/上半场 独赢 | 无 |
| `AH(<line>)` | 全场 让球（两向） | 主队让球线（有符号） |
| `AH_1H(<line>)` | 上半场 让球 | 同上 |
| `OU(<line>)` | 全场 大小 | 大小球线 |
| `OU_1H(<line>)` | 上半场 大小 | 同上 |

线值格式：`0/0.5`（复合盘，取中点 0.25）、`-0.5`、`2.5` 等。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional, Tuple

__all__ = [
    "FAMILY_NAMES",
    "WINNER_OUTCOME_ZH",
    "TOTAL_OUTCOME_ZH",
    "parse_market_code",
    "format_market",
    "format_pick",
    "describe_market",
    "ah_side_label",
]

#: 盘口族 → 中文名（不含线值）
FAMILY_NAMES: Mapping[str, str] = {
    "HAD": "独赢",
    "AH": "让球",
    "OU": "大小",
}

#: 半场前缀
_HALF_PREFIX = "上半场"
_FULL_PREFIX = "全场"

#: `AH_1H(0.25)` / `OU(2.5)` / `HAD_1H` / `HAD` 的解析
_CODE_RE = re.compile(
    r"^(?P<fam>HAD|AH|OU)(?P<half>_1H)?(?:\((?P<line>[^)]*)\))?$", re.I)


def parse_market_code(code: object) -> Optional[Tuple[str, bool, str]]:
    """解析盘口代码 → `(族, 是否上半场, 线值字符串)`；无法解析返回 None。

    >>> parse_market_code("OU_1H(1.5)")
    ('OU', True, '1.5')
    >>> parse_market_code("HAD")
    ('HAD', False, '')
    """
    if not isinstance(code, str):
        return None
    m = _CODE_RE.match(code.strip())
    if not m:
        return None
    return (m.group("fam").upper(), bool(m.group("half")),
            (m.group("line") or "").strip())


def _line_zh(line: str) -> str:
    """线值展示（保留行业习惯写法）。

    复合盘 `0/0.5`、`1/1.5` **原样保留** —— 它是行业通用写法，
    而中点（0.25）只是计算用的近似，展示中点会让人困惑。
    负号由「让/受让」表述，这里去掉避免双重否定。
    """
    s = str(line or "").strip()
    return s.lstrip("+")


#: 独赢盘的结果 → 中文（`ot` 是乐鱼原始字段值，一并兼容）
WINNER_OUTCOME_ZH: Mapping[str, str] = {
    "home": "主胜",
    "draw": "平局",
    "away": "客胜",
    # 兼容乐鱼原始 ot 取值
    "1": "主胜", "X": "平局", "2": "客胜",
}
#: 大小球结果 → 中文
TOTAL_OUTCOME_ZH: Mapping[str, str] = {
    "over": "大", "under": "小",
    "Over": "大", "Under": "小",
}
#: 兼容旧名（内部引用）
_WINNER_OUTCOME = WINNER_OUTCOME_ZH
_TOTAL_OUTCOME = TOTAL_OUTCOME_ZH


def format_market(market: object, outcome: object = "",
                  line: object = "") -> str:
    """把 `(盘口代码, 结果)` 格式化为中文描述。

    例（全部来自本项目真实盘口）：

        format_market("OU_1H(1.5)", "over")   -> "上半场大1.5"
        format_market("OU(2.5)", "under")     -> "全场小2.5"
        format_market("AH_1H(0.25)", "home", "0/0.5")
                                              -> "上半场主队让0/0.5"
        format_market("AH(-0.5)", "home")     -> "全场主队让0.5"
        format_market("HAD_1H", "home")       -> "上半场主胜"
        format_market("HAD", "draw")          -> "全场平局"

    Args:
        market: 盘口代码（如 `OU_1H(1.5)`）。
        outcome: 结果名（`home`/`draw`/`away`/`over`/`under`）。
        line: **原始线值**（如 `0/0.5`）；给出时优先于代码里的中点值，
            因为中点（0.25）只是计算近似，展示会让人困惑。

    Returns:
        中文描述；无法识别时回退为原始的 `"代码 结果"`，**不抛异常**
        （展示层不应因为一个陌生盘口就崩掉）。
    """
    parsed = parse_market_code(market)
    oc = str(outcome or "").strip()
    if parsed is None:
        return ("%s %s" % (market, oc)).strip()
    fam, is_half, code_line = parsed
    ln = str(line or "").strip() or code_line
    prefix = _HALF_PREFIX if is_half else _FULL_PREFIX

    if fam == "HAD":
        oc_zh = _WINNER_OUTCOME.get(oc, oc)
        return "%s%s" % (prefix, oc_zh) if oc_zh else "%s独赢" % prefix

    if fam == "OU":
        oc_zh = _TOTAL_OUTCOME.get(oc, oc)
        return "%s%s%s" % (prefix, oc_zh, _line_zh(ln)) if oc_zh \
            else "%s大小%s" % (prefix, _line_zh(ln))

    # AH 让球：line 是**主队让球线**（有符号）
    #   line < 0 → 主队让球；line > 0 → 主队受让
    # 用「让/受让」表述方向，而不是把负号丢给用户看。
    return "%s%s" % (prefix, ah_side_label(oc, ln))


def ah_side_label(outcome: object, line: object) -> str:
    """让球盘的「哪方让/受让多少」描述（不含半场前缀）。

    语义（`line` 是**主队让球线**，有符号）：

        line < 0 → 主队让球 ⇒ 投注主队是「主队让」，投注客队是「客队受让」
        line > 0 → 主队受让 ⇒ 投注主队是「主队受让」，投注客队是「客队让」
        line = 0 → 平手盘，无方向

    Args:
        outcome: `home` / `away`。
        line: 主队让球线。支持复合盘 `0/0.5`、`-0.5/1`（取首段判方向）。

    >>> ah_side_label("home", "-0.5")
    '主队让0.5'
    >>> ah_side_label("away", "-0.5")
    '客队受让0.5'
    >>> ah_side_label("home", "0.5")
    '主队受让0.5'
    >>> ah_side_label("away", "0.5")
    '客队让0.5'
    """
    oc = str(outcome or "").strip()
    raw = str(line or "").strip()
    if oc not in ("home", "away"):
        return "让球%s" % raw
    side_cn = "主队" if oc == "home" else "客队"
    if not raw:
        return "%s让球" % side_cn

    first = raw.split("/")[0].strip()
    mag = raw.lstrip("+-")          # 去掉符号（方向由让/受让表达）
    try:
        sign = float(first)
    except ValueError:
        return "%s让球%s" % (side_cn, raw)

    if sign == 0.0 and "/" not in raw:
        # 纯整数 0（平手盘）：无方向
        return "%s平手" % side_cn
    # 复合盘如 `0/0.5`：首段为 0 但整体有方向（后半段是让球方向）。
    # 此时不能用首段判定“平手”，应以**非零段**的符号为准。
    if sign == 0.0:
        tail = raw.split("/")[1].strip() if "/" in raw else "0"
        try:
            sign = float(tail.lstrip("+"))
        except ValueError:
            sign = 0.0
    if sign == 0.0:
        return "%s平手" % side_cn
    # 主队让球（sign<0）时：主队为让方，客队为受让方；反之相反。
    home_gives = sign < 0
    is_giver = (oc == "home") == home_gives
    return "%s%s%s" % (side_cn, "让" if is_giver else "受让", mag)


def format_pick(market: object, outcome: object, line: object = "") -> str:
    """决策结论用的完整描述（与 `format_market` 同义，语义更明确的别名）。"""
    return format_market(market, outcome, line)


def describe_market(market: object) -> str:
    """只描述盘口本身（不带结果）。

    >>> describe_market("OU_1H(1.5)")
    '上半场大小 1.5'
    """
    parsed = parse_market_code(market)
    if parsed is None:
        return str(market)
    fam, is_half, ln = parsed
    prefix = _HALF_PREFIX if is_half else _FULL_PREFIX
    name = FAMILY_NAMES.get(fam, fam)
    return ("%s%s %s" % (prefix, name, ln)).strip()


def label_from_snapshot(market: object, outcome: object,
                        line: object = "") -> str:
    """便捷包装：优先用显式 line，否则从代码解析。"""
    return format_market(market, outcome, line)
