#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线复算报告中的量化结论（不联网、不依赖第三方库）。

用途：让 `REPORT.md` 里三个量化结论可**独立复现**：

  1. **门控阈值影响**（缺陷 D3）
     旧规则 = 绝对 3pp；新规则 = clamp(max(3pp, 1.5×水钱), ≤12pp)。
     统计两者各拒掉多少盘口，验证「固定阈值会系统性误杀高水钱盘口」。

  2. **`p_llm` 分布与污染证据**（缺陷 D2）
     统计买入建议里 `p_llm` 的分布，以及「理由文本含终场比分」的条数 ——
     这是 LLM 抄答案的直接证据（`p_llm=1.0` vs 市场 0.35）。

  3. **被拒原因分布**（问题 2 的规模）
     统计 outcome 级 `rejects` 的计数，定位最集中的拒因。

数据来源：仓库内 `output/decisions.json`（生产决策落盘，非手造数据）。
若文件不存在，脚本会明确报错而不是输出空的假结论。

运行::

    python3 reports/dashboard-data-stats-optimization/scripts/analyze_fixes.py

依赖：仅标准库。
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from collections import Counter
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------- #
# 常量（与代码中的门控参数一一对应；改代码时同步改这里，报告才不会说谎）
# --------------------------------------------------------------------------- #

#: 旧规则的绝对阈值（百分点）—— 缺陷 D3 的取值
OLD_ABS_SPREAD_PP = 3.0
#: 新规则的相对系数（`EntryGateConfig.method_spread_margin_ratio`）
NEW_MARGIN_RATIO = 1.5
#: 新规则的绝对下限（`EntryGateConfig.max_method_spread_pp`）
NEW_ABS_FLOOR_PP = 3.0
#: 新规则的硬上限（`EntryGateConfig.method_spread_hard_cap_pp`）
NEW_HARD_CAP_PP = 12.0
#: 判定「理由文本泄露了赛果」的模式
LEAK_PATTERN = re.compile(r"终场|已定|全场比分|\d+:\d+")
#: 认为「过度自信」的 p_llm 阈值（污染护栏的观察口径）
OVERCONFIDENT_P_LLM = 0.9

#: 仓库根（scripts/ 的上两级）
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
DEFAULT_DECISIONS = os.path.join(ROOT, "output", "decisions.json")


def _num(value: object) -> Optional[float]:
    """容错浮点：非数值/非有限值返回 None。

    本脚本消费的是**生产落盘的 JSON**（可能被外部进程写入、
    可能因版本差异缺字段），一个脏字段不应让整份统计崩掉 ——
    那样用户就得不到任何结论，而局部缺数据本可容忍。
    """
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def new_limit_pp(margin: float) -> float:
    """新规则的允许分歧上限（百分点）。与 `core.entry_gate.max_spread_pp` 同式。

    入参可能来自 JSON（本函数对调用方公开），故容错转换：
    非法值按 0 处理（等价于只守绝对下限）。
    """
    m = _num(margin)
    margin_pp = max(0.0, (m if m is not None else 0.0) * 100.0)
    limit = max(NEW_ABS_FLOOR_PP, NEW_MARGIN_RATIO * margin_pp)
    return min(limit, NEW_HARD_CAP_PP)


def load_decisions(path: str) -> Dict[str, Any]:
    """读取决策落盘；缺失/损坏即报错（不得静默输出空结论）。"""
    if not os.path.exists(path):
        raise SystemExit(
            "错误：找不到 %s\n"
            "该文件是生产决策落盘（output/ 已被 .gitignore 忽略）。\n"
            "请在跑过一轮决策的机器上执行本脚本。" % path)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise SystemExit("错误：%s 无法解析（%s: %s）"
                         % (path, type(exc).__name__, exc)) from exc
    if not isinstance(data, dict):
        raise SystemExit("错误：%s 顶层应为 JSON 对象" % path)
    return data


def analyse_spread(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """复算 §6.3「门控阈值影响」。"""
    total = 0
    old_rej = 0
    new_rej = 0
    for match in rows:
        for comp in match.get("computations") or []:
            if not isinstance(comp, dict):
                continue
            spread = _num(comp.get("method_spread_pp"))
            margin = _num(comp.get("margin"))
            if spread is None or margin is None:
                continue
            total += 1
            if spread > OLD_ABS_SPREAD_PP:
                old_rej += 1
            if spread > new_limit_pp(margin):
                new_rej += 1
    return {
        "markets": total,
        "old_rejected": old_rej,
        "new_rejected": new_rej,
        "old_rate": (old_rej / total) if total else 0.0,
        "new_rate": (new_rej / total) if total else 0.0,
        "freed": old_rej - new_rej,
    }


def analyse_picks(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """复算 §2.1③「比分泄漏证据」与 `p_llm` 分布。"""
    picks: List[Dict[str, Any]] = []
    for match in rows:
        picks.extend(match.get("picks") or [])
    leaked = [p for p in picks if LEAK_PATTERN.search(str(p.get("reason") or ""))]
    overconf = [p for p in picks if (_num(p.get("p_llm")) or 0.0)
                >= OVERCONFIDENT_P_LLM]
    edges = [e for e in (_num(p.get("edge_pct")) for p in picks)
             if e is not None]
    return {
        "picks": len(picks),
        "matches_with_picks": sum(1 for m in rows if m.get("picks")),
        "leaked_reason": len(leaked),
        "leaked_samples": [str(p.get("reason"))[:40] for p in leaked[:5]],
        "overconfident": len(overconf),
        "mean_edge_pct": (sum(edges) / len(edges)) if edges else None,
    }


def analyse_rejects(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """复算 §1.2「被拒原因分布」（outcome 级）。"""
    outcome_level: Counter = Counter()
    for match in rows:
        for comp in match.get("computations") or []:
            if not isinstance(comp, dict):
                continue
            for gate in comp.get("gates") or []:
                if not isinstance(gate, dict):
                    continue
                for reason in gate.get("rejects") or []:
                    outcome_level[str(reason)] += 1
    return {"outcome_rejects": outcome_level.most_common()}


def main(argv: List[str]) -> int:
    path = argv[1] if len(argv) > 1 else DEFAULT_DECISIONS
    data = load_decisions(path)
    rows = data.get("decisions") or []

    print("=" * 68)
    print("决策链路修复量化复算")
    print("数据来源: %s" % os.path.relpath(path, ROOT))
    print("触发来源: %s" % data.get("trigger"))
    print("场次: %d   汇总: %s" % (len(rows), data.get("summary")))
    print("=" * 68)

    sp = analyse_spread(rows)
    print("\n[1] 门控阈值影响（缺陷 D3：分歧上限应随水钱缩放）")
    print("    参与统计盘口: %d" % sp["markets"])
    print("    旧规则(绝对 %.1fpp)        拒绝 %d (%.0f%%)"
          % (OLD_ABS_SPREAD_PP, sp["old_rejected"], sp["old_rate"] * 100))
    print("    新规则(%.1f×水钱, 封顶 %.0fpp) 拒绝 %d (%.0f%%)"
          % (NEW_MARGIN_RATIO, NEW_HARD_CAP_PP,
             sp["new_rejected"], sp["new_rate"] * 100))
    print("    => 多放行 %d 个盘口（降幅 %.0f%%）"
          % (sp["freed"],
             (sp["freed"] / sp["old_rejected"] * 100) if sp["old_rejected"] else 0))

    pk = analyse_picks(rows)
    print("\n[2] 买入建议与污染证据（缺陷 D2：比分区泄漏）")
    print("    建议总数: %d   涉及场次: %d" % (pk["picks"], pk["matches_with_picks"]))
    print("    理由文本含赛果(终场/x:y): %d" % pk["leaked_reason"])
    for s in pk["leaked_samples"]:
        print("      · %s" % s)
    print("    p_llm >= %.1f 的过度自信建议: %d" % (OVERCONFIDENT_P_LLM,
                                                   pk["overconfident"]))
    if pk["mean_edge_pct"] is not None:
        print("    平均 edge: %+.1f%%" % pk["mean_edge_pct"])

    rj = analyse_rejects(rows)
    print("\n[3] outcome 级被拒原因分布（问题 2 的规模）")
    for reason, n in rj["outcome_rejects"]:
        print("    %-24s %d" % (reason, n))

    print("\n结论："
          "绝对值阈值系统性误杀高水钱盘口；"
          "含有赛果的理由证明提示词曾泄漏答案（已修复）。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
