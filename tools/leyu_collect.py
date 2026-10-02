#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
乐鱼（leyu）赛事 + 盘口采集 CLI。

支持两种模式：

1. **在线**（默认）：真实请求网关，拉全量赛程与实时盘口，可选订阅 WS。
2. **离线回放**：从 `leyu.saz` 回放，无需网络，用于协议回归与演示。

用法::

    # 全量赛程概览（1868 场 / 333 联赛）
    python3 tools/leyu_collect.py schedule

    # 拉指定赛事（或其所属联赛）的完整盘口
    python3 tools/leyu_collect.py odds --mids 5714088,5687518

    # 拉全部进行中赛事的盘口
    python3 tools/leyu_collect.py odds --live --limit 40

    # 实时推送（需网络）
    python3 tools/leyu_collect.py watch --mids 5714088 --seconds 60

    # 离线回放抓包（无网络依赖）
    python3 tools/leyu_collect.py schedule --replay leyu.saz --sid 347
    python3 tools/leyu_collect.py odds     --replay leyu.saz --sid 426

输出：`--json <path>` 落盘；默认打印人类可读摘要。

本脚本只读数据，不实现任何投注/下单能力（AGENTS.md §0.5）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import zipfile
from typing import Any, Dict, List, Optional, Sequence

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from collector.leyu_client import (  # noqa: E402
    DEFAULT_HOST,
    DEFAULT_ORIGIN,
    DecodeError,
    LeYuClient,
    LeYuMatch,
    LeYuError,
    TransportError,
    decode_envelope,
    parse_match_list,
    parse_odds_block,
)
from collector.leyu_ws import LeYuFeed  # noqa: E402

DEFAULT_SAZ = os.path.join(_ROOT, "leyu.saz")

# 抓包中已验证的会话 → 用途映射（离线回放时的默认样本）
REPLAY_SIDS = {
    "schedule": 347,   # GET  /yewu11/v2/m/getOriginalDataPB
    "odds": 426,       # POST /yewu11/v1/w/structureMatchBaseInfoByMidsPB
}


# --------------------------------------------------------------------------- #
# 离线回放
# --------------------------------------------------------------------------- #

def _saz_body(path: str, sid: int) -> str:
    """读取 SAZ 会话的响应体（兼容 CRLF/LF 两种分隔）。"""
    with zipfile.ZipFile(path) as z:
        text = z.read("raw/%03d_s.txt" % sid).decode("utf-8", "replace")
    idx = text.find("\r\n\r\n")
    if idx < 0:
        idx = text.find("\n\n")
        return text[idx + 2:] if idx >= 0 else ""
    return text[idx + 4:]


def replay(path: str, kind: str, sid: Optional[int] = None) -> List[LeYuMatch]:
    """从 saz 回放一次采集（不发起任何网络请求）。"""
    use_sid = sid if sid is not None else REPLAY_SIDS[kind]
    if not os.path.exists(path):
        raise LeYuError("抓包文件不存在: %s" % path)
    try:
        body = _saz_body(path, use_sid)
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise LeYuError("读取会话 %03d 失败: %s" % (use_sid, exc)) from exc
    if not body:
        raise LeYuError("会话 %03d 无响应体" % use_sid)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DecodeError("会话 %03d 响应体非 JSON: %s" % (use_sid, exc)) from exc
    blob = decode_envelope(payload)
    return parse_match_list(blob) if kind == "schedule" else parse_odds_block(blob)


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #

def _print_schedule(matches: Sequence[LeYuMatch]) -> None:
    sports: Dict[str, int] = {}
    leagues: Dict[str, int] = {}
    states = {"未开赛": 0, "进行中": 0, "已结束": 0, "其它": 0}
    for m in matches:
        sports[m.sport or m.sport_id] = sports.get(m.sport or m.sport_id, 0) + 1
        leagues[m.tournament or m.tid] = leagues.get(m.tournament or m.tid, 0) + 1
        if m.is_live:
            states["进行中"] += 1
        elif m.is_finished:
            states["已结束"] += 1
        elif m.status == 0:
            states["未开赛"] += 1
        else:
            states["其它"] += 1

    print("赛事总数: %d" % len(matches))
    print("运动项  : %d  %s" % (len(sports), dict(sorted(sports.items(), key=lambda kv: -kv[1])[:6])))
    print("联赛数  : %d" % len(leagues))
    print("状态分布: %s" % states)
    print("\n== 进行中赛事（前 15）==")
    live = [m for m in matches if m.is_live]
    for m in live[:15]:
        score = m.score
        shown = "%s:%s" % score if score[0] is not None else "-"
        print("  %-10s %-14s %-12s %s  %-8s %s"
              % (m.mid, m.tournament[:14], m.home[:12], shown, m.clock, m.away[:12]))
    if len(live) > 15:
        print("  … 另有 %d 场" % (len(live) - 15))


def _print_odds(matches: Sequence[LeYuMatch]) -> None:
    with_odds = [m for m in matches if m.markets]
    print("赛事数: %d（其中有盘口 %d）" % (len(matches), len(with_odds)))
    for m in with_odds[:5]:
        print("\n%s  %s vs %s  （开赛 %s）"
              % (m.mid, m.home, m.away,
                 time.strftime("%m-%d %H:%M", time.localtime(m.start_ms / 1000))))
        for mk in m.markets:
            print("  [%s] %-12s hpt=%d 线=%-8s 水位=%+.2f%%"
                  % (mk.chpid, mk.name, mk.hpt, mk.hv or "-", mk.margin * 100))
            for q in mk.quotes:
                print("        %-6s %-12s 赔率=%-8.3f %s"
                      % (q.outcome, q.label, q.decimal, q.source))
    if len(with_odds) > 5:
        print("\n… 另有 %d 场（用 --json 落盘查看全部）" % (len(with_odds) - 5))


def _dump(matches: Sequence[LeYuMatch], path: str) -> None:
    rows: List[Dict[str, Any]] = []
    for m in matches:
        rows.append({
            "mid": m.mid,
            "sport": m.sport,
            "tournament": m.tournament,
            "home": m.home,
            "away": m.away,
            "start_ms": m.start_ms,
            "status": m.status,
            "minute": m.minute,
            "score": m.score,
            "markets": [
                {
                    "chpid": mk.chpid, "name": mk.name, "hpt": mk.hpt, "line": mk.hv,
                    "margin": mk.margin,
                    "quotes": [
                        {"oid": q.oid, "outcome": q.outcome, "label": q.label,
                         "decimal": q.decimal, "line": q.line, "malay": q.malay,
                         "source": q.source, "ctsp": q.ctsp}
                        for q in mk.quotes
                    ],
                }
                for mk in m.markets
            ],
        })
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)
    except OSError as exc:
        raise LeYuError("写出失败 %s: %s" % (path, exc)) from exc
    print("已写出 %d 场 → %s" % (len(rows), path))


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def _build_client(args: argparse.Namespace) -> LeYuClient:
    return LeYuClient(
        host=args.host,
        origin=args.origin,
        request_id=args.request_id,
        timeout=args.timeout,
    )


def cmd_schedule(args: argparse.Namespace) -> int:
    if args.replay:
        matches = replay(args.replay, "schedule", args.sid)
    else:
        matches = _build_client(args).all_matches()
    _print_schedule(matches)
    if args.json:
        _dump(matches, args.json)
    return 0


def cmd_odds(args: argparse.Namespace) -> int:
    if args.replay:
        matches = replay(args.replay, "odds", args.sid)
    else:
        client = _build_client(args)
        mids = [m for m in (args.mids or "").split(",") if m]
        if args.live or not mids:
            schedule = client.all_matches()
            pool = [m.mid for m in schedule if m.is_live] if args.live \
                else [m.mid for m in schedule if not m.is_finished]
            mids = pool[: args.limit]
            print("选中 %d 场（共 %d 场候选）" % (len(mids), len(pool)))
        matches = []
        for batch in client.iter_odds_batches(mids, batch_size=args.batch):
            matches.extend(batch)
    _print_odds(matches)
    if args.json:
        _dump(matches, args.json)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    client = _build_client(args)
    mids = [m for m in (args.mids or "").split(",") if m]
    if not mids:
        mids = [m.mid for m in client.all_matches() if m.is_live][: args.limit]
    if not mids:
        print("没有可订阅的赛事（--mids 为空且无进行中赛事）", file=sys.stderr)
        return 1
    print("WS: %s" % client.ws_url())
    print("订阅 %d 场: %s" % (len(mids), ",".join(mids)))
    seen = 0
    deadline = time.monotonic() + args.seconds
    feed = LeYuFeed(client.ws_url(), client.request_id, client.origin, timeout=args.timeout)
    try:
        feed.connect()
        feed.subscribe_odds(mids, cufm=args.cufm)
        while time.monotonic() < deadline:
            message = feed.recv()
            if message is None:
                continue
            seen += 1
            print("[%s] cmd=%s %s"
                  % (time.strftime("%H:%M:%S"), message.get("cmd"),
                     json.dumps(message, ensure_ascii=False)[:300]))
            if args.max_messages and seen >= args.max_messages:
                break
    except (LeYuError, OSError, KeyboardInterrupt) as exc:
        print("推送中断: %s" % exc, file=sys.stderr)
        return 1
    finally:
        feed.close()
    print("共收到 %d 条消息（stats=%s）" % (seen, feed.stats))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="乐鱼赛事/盘口采集（只读）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--host", default=DEFAULT_HOST, help="网关（prod.json 解密后可得）")
        p.add_argument("--origin", default=DEFAULT_ORIGIN)
        p.add_argument("--request-id", default=None, help="32 位 hex，默认随机")
        p.add_argument("--timeout", type=float, default=15.0)
        p.add_argument("--json", help="结果落盘为 JSON")
        p.add_argument("--replay", nargs="?", const=DEFAULT_SAZ, default=None,
                       help="离线回放 saz（默认 ./leyu.saz）")
        p.add_argument("--sid", type=int, default=None, help="回放指定会话号")

    p1 = sub.add_parser("schedule", help="全量赛程")
    add_common(p1)
    p1.set_defaults(func=cmd_schedule)

    p2 = sub.add_parser("odds", help="实时盘口")
    add_common(p2)
    p2.add_argument("--mids", help="逗号分隔赛事 ID")
    p2.add_argument("--live", action="store_true", help="只取进行中赛事")
    p2.add_argument("--limit", type=int, default=40)
    p2.add_argument("--batch", type=int, default=20)
    p2.set_defaults(func=cmd_odds)

    p3 = sub.add_parser("watch", help="WS 实时推送")
    add_common(p3)
    p3.add_argument("--mids", help="逗号分隔赛事 ID")
    p3.add_argument("--limit", type=int, default=20)
    p3.add_argument("--seconds", type=float, default=60.0)
    p3.add_argument("--max-messages", type=int, default=0)
    p3.add_argument("--cufm", default="L", choices=["L", "LM"])
    p3.set_defaults(func=cmd_watch)

    args = ap.parse_args(argv)
    try:
        return int(args.func(args))
    except (LeYuError, TransportError, DecodeError) as exc:
        print("采集失败: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
