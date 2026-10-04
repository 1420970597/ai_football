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
    "format_selection",
    "format_pick",
    "describe_market",
    "ah_side_label",
    "negate_line",
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

    复合盘 `0/0.5`、`1/1.5` **原样保留** —— 它是行业通用写法
    （实测乐鱼 `hv` 就是这种形式），而中点（0.25）只是计算用的近似，
    展示中点会让人困惑。
    """
    s = str(line or "").strip()
    return s.lstrip("+")


def _is_zero_line(line: str) -> bool:
    """线值是否所有分段都为 0（如 `0`、`0/0`）。平手盘无让球方向。"""
    parts = [p.strip().lstrip("+") for p in str(line or "").split("/")]
    parts = [p for p in parts if p]
    if not parts:
        return False
    try:
        return all(float(p) == 0.0 for p in parts)
    except ValueError:
        return False


def negate_line(line: object) -> str:
    """把让球线**取反**（主队让球线 ↔ 客队让球线）。

    乐鱼的 `hv` 是**主队视角**的有符号让球线，所以客队那一侧必须取反
    才能正确展示 —— 否则「主队让1」会被错标成「客队让1」
    （方向搞反，比不展示更危险）。

    复合盘只翻转整体符号（乐鱼实测 `-0/0.5` ↔ `0/0.5`），
    不逐段处理（逐段会写出 `0/-0.5` 这种上游不存在的写法）。

    >>> negate_line("-1")
    '+1'
    >>> negate_line("0/0.5")
    '-0/0.5'
    >>> negate_line("0")
    '0'
    """
    s = str(line or "").strip()
    if not s:
        return ""
    if _is_zero_line(s):
        return "0"                     # 平手盘：取反仍是平手，不写 `-0`
    if s.startswith("-"):
        return s[1:]
    if s.startswith("+"):
        return "-" + s[1:]
    return "-" + s


def _signed_line(line: object) -> str:
    """让球线展示：**正数补显式 `+`**（乐鱼风格 `主队上半场-1` / `客队上半场+1`）。

    为何要显式符号：让球盘只说「1」无法区分让/受让，是方向歧义；
    `+1`/`-1` 与乐鱼界面一致，也避免与 `ah_side_label` 的
    「让/受让」措辞相互干扰。平手盘（0）不加符号。
    """
    s = str(line or "").strip()
    if not s:
        return ""
    if _is_zero_line(s):
        return "0"
    if s.startswith(("-", "+")):
        return s
    return "+" + s


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
                  line: object = "", home: object = "",
                  away: object = "") -> str:
    """把 `(盘口代码, 结果)` 格式化为**乐鱼风格中文**描述。

    用户要求：盘口信息与买入建议一律说中文，且与乐鱼界面一致：

        xx队上半场-1        ← 让球（带队名 + 带符号线值）
        上半场进球数>1/1.5  ← 大小球（用 `>` / `<`）

    例（全部取自本项目真实盘口）：

        format_market("AH_1H(-1)", "home", "-1", "曼联", "利物浦")
                                        -> "曼联上半场-1"
        format_market("AH_1H(-1)", "away", "-1", "曼联", "利物浦")
                                        -> "利物浦上半场+1"
        format_market("OU_1H(1.25)", "over", "1/1.5")
                                        -> "上半场进球数>1/1.5"
        format_market("OU(2.5)", "under")   -> "全场进球数<2.5"
        format_market("HAD_1H", "home")      -> "上半场主胜"

    Args:
        market: 盘口代码（如 `OU_1H(1.5)`）。
        outcome: 结果名（`home`/`draw`/`away`/`over`/`under`）。
        line: **原始线值**（如 `0/0.5`）；给出时优先于代码里的中点值，
            因为中点（0.25）只是计算近似，展示会让人困惑。
        home / away: 主/客队名。给出时用于让球盘的「队名」前缀
            （如 `曼联上半场-1`）；缺失则回退为「主队/客队」。

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
        # 乐鱼风格：用比较符表达大/小，而不是「大 2.5」。
        #   over  → `>线值`（实际进球数大于该线）
        #   under → `<线值`
        # 这样展示与乐鱼界面的「进球数 > 1/1.5」一致。
        body = _line_zh(ln)
        if oc in ("over", "Over"):
            return "%s进球数>%s" % (prefix, body)
        if oc in ("under", "Under"):
            return "%s进球数<%s" % (prefix, body)
        return "%s进球数%s" % (prefix, body)

    # AH 让球（乐鱼风格：`<队名><半场><带符号线值>`）
    #
    # ⚠️ 关键：`line` 是**主队视角**的有符号让球线（乐鱼 `hv`）。
    # 因此客队那一侧必须取反，否则会把「主队让1」错标成「客队让1」——
    # 方向搞反比不展示更危险。
    if oc == "away":
        ln = negate_line(ln)
    name = ""
    if oc == "home":
        name = str(home or "").strip() or "主队"
    elif oc == "away":
        name = str(away or "").strip() or "客队"
    return "%s%s%s" % (name, prefix, _signed_line(ln))


def format_selection(market: object, outcome: object = "",
                     line: object = "", home: object = "",
                     away: object = "") -> str:
    """「选项」级中文描述（带队名）。语义上等价于 `format_market`。

    单独立名是为了让调用点读起来清楚：本函数描述的是**一个可投注选项**
    （如「曼联上半场-1」），而不是整个盘口（「上半场让球 -1」）。
    """
    return format_market(market, outcome, line, home=home, away=away)


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
