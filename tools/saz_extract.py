#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SAZ (Fiddler Session Archive) 结构化导出器 —— 只读、零第三方依赖。

用途：把 .saz 里的每个 HTTP 会话导成一条 JSON 记录，供后续取证 / 协议还原。
本脚本**不发起任何网络请求**，仅解析本地归档。

用法：
    python3 tools/saz_extract.py leyu.saz                 # 概要统计
    python3 tools/saz_extract.py leyu.saz --grep getMatch # 过滤 URL
    python3 tools/saz_extract.py leyu.saz --sid 42 --full # 打印某会话全文
    python3 tools/saz_extract.py leyu.saz --jsonl out.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from typing import Dict, Iterator, List, Optional

_ENTRY_RE = re.compile(r"^raw/(\d+)_([csmw])\.(txt|xml)$")
_REQ_LINE_RE = re.compile(r"^(GET|POST|PUT|DELETE|HEAD|OPTIONS|PATCH|CONNECT)\s+(\S+)\s+HTTP/([\d.]+)", re.M)
_HEADER_RE = re.compile(r"^([A-Za-z0-9\-]+):\s*(.*)$")
# HTTP 头/体分隔符：兼容 CRLF 与 LF 两种归档写法
_HEAD_BODY_SEP_RE = re.compile(r"\r?\n\r?\n")


def _to_int(value: object, default: int = 0) -> int:
    """宽松整数转换，非法输入回退到 default（不抛异常）。"""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def split_head_body(block: str) -> tuple[str, str]:
    """按 HTTP 语义切分 head/body（兼容 SAZ 的 CRLF 与 LF）。"""
    m = _HEAD_BODY_SEP_RE.search(block)
    if not m:
        return block, ""
    return block[: m.start()], block[m.end():]


def _split_headers(block: str) -> Dict[str, str]:
    """把 Header 块解析为 dict（同名 header 后者覆盖前者）。"""
    out: Dict[str, str] = {}
    for line in block.splitlines():
        m = _HEADER_RE.match(line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def iter_sessions(saz_path: str) -> Iterator[Dict[str, object]]:
    """遍历 SAZ 内的所有会话，产出原始记录。"""
    with zipfile.ZipFile(saz_path) as z:
        buckets: Dict[str, Dict[str, bytes]] = {}
        for name in z.namelist():
            m = _ENTRY_RE.match(name)
            if not m:
                continue
            sid, kind, _ext = m.groups()
            buckets.setdefault(sid, {})[kind] = z.read(name)

        def _txt(sid: str, kind: str) -> str:
            raw = buckets.get(sid, {}).get(kind)
            return raw.decode("utf-8", "replace") if raw else ""

        for sid in sorted(buckets, key=_to_int):
            yield {
                "sid": _to_int(sid),
                "meta": _txt(sid, "m"),
                "client": _txt(sid, "c"),
                "server": _txt(sid, "s"),
                "raw_hex": buckets.get(sid, {}).get("w"),
            }


def parse_session(rec: Dict[str, object]) -> Dict[str, object]:
    """把一个原始会话解析成结构化 dict。"""
    client: str = str(rec.get("client") or "")
    server: str = str(rec.get("server") or "")
    meta: str = str(rec.get("meta") or "")

    def _flag(n: str, src: str) -> Optional[str]:
        m = re.search(rf'<SessionFlag N="{re.escape(n)}" V="([^"]*)"', src)
        return m.group(1) if m else None

    req_line = _REQ_LINE_RE.search(client)
    method = req_line.group(1) if req_line else None
    url = req_line.group(2) if req_line else None
    version = req_line.group(3) if req_line else None

    # 请求/响应头块
    req_headers: Dict[str, str] = {}
    req_body = ""
    if req_line:
        rest = client[req_line.end():]
        head, body = split_head_body(rest)
        req_headers = _split_headers(head)
        req_body = body.strip("\r\n")

    status = None
    resp_headers: Dict[str, str] = {}
    resp_body = ""
    st = re.match(r"^HTTP/[\d.]+\s+(\d{3})", server.lstrip("\ufeff \r\n\t"))
    if st:
        status = _to_int(st.group(1), -1)
        rest = server[st.end():]
        head, body = split_head_body(rest)
        resp_headers = _split_headers(head)
        resp_body = body

    host = req_headers.get("Host") or _flag("https-client-snihostname", meta) or ""
    if method == "CONNECT" and url:
        host = url

    return {
        "sid": rec["sid"],
        "host": host.split(":")[0],
        "host_port": host,
        "method": method,
        "url": url,
        "http_version": version,
        "status": status,
        "process": _flag("x-processinfo", meta),
        "sni": _flag("https-client-snihostname", meta),
        "tls": _flag("https-server-version", meta),
        "req_headers": req_headers,
        "req_body": req_body,
        "resp_headers": resp_headers,
        "resp_body": resp_body,
        "resp_bytes": len(resp_body),
        "req_bytes": len(req_body),
        "timers": meta,
    }


def _summary(rows: List[Dict[str, object]]) -> None:
    hosts: Dict[str, int] = {}
    for r in rows:
        hosts[str(r["host"])] = hosts.get(str(r["host"]), 0) + 1
    print(f"会话总数: {len(rows)}")
    print("\n== 主机分布 ==")
    for h, n in sorted(hosts.items(), key=lambda kv: -kv[1]):
        print(f"{n:5d}  {h}")
    print("\n== 带响应体的 API 端点 ==")
    seen: Dict[str, Dict[str, int]] = {}
    for r in rows:
        if not r["url"] or str(r["url"]).startswith("CONNECT"):
            continue
        path = str(r["url"]).split("?")[0]
        key = f"{r['method']} {path}"
        slot = seen.setdefault(key, {"n": 0, "ok": 0, "json": 0, "max": 0})
        slot["n"] += 1
        if r["status"] == 200:
            slot["ok"] += 1
        body = str(r["resp_body"])
        if body[:1] in "{[":
            slot["json"] += 1
        slot["max"] = max(slot["max"], _to_int(r["resp_bytes"]))
    for key, s in sorted(seen.items(), key=lambda kv: -kv[1]["n"]):
        print(f"{s['n']:4d}x ok={s['ok']:<4d} json={s['json']:<4d} max={s['max']:<8d} {key}")


def main() -> int:
    ap = argparse.ArgumentParser(description="SAZ 结构化导出（只读）")
    ap.add_argument("saz")
    ap.add_argument("--grep", help="URL 子串过滤")
    ap.add_argument("--sid", type=int, help="只输出该会话")
    ap.add_argument("--full", action="store_true", help="打印请求/响应全文")
    ap.add_argument("--jsonl", help="导出为 JSONL")
    args = ap.parse_args()

    rows = [parse_session(r) for r in iter_sessions(args.saz)]
    if args.grep:
        rows = [r for r in rows if args.grep in str(r["url"])]
    if args.sid is not None:
        rows = [r for r in rows if r["sid"] == args.sid]

    if args.jsonl:
        try:
            with open(args.jsonl, "w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        except OSError as exc:
            print(f"写出失败: {exc}", file=sys.stderr)
            return 2
        print(f"已写出 {len(rows)} 条 → {args.jsonl}")
        return 0

    if args.full:
        for r in rows:
            print("=" * 78)
            print(f"SID {r['sid']}  {r['method']} {r['url']}  -> {r['status']}")
            print(json.dumps(r["req_headers"], ensure_ascii=False, indent=1))
            if r["req_body"]:
                print("--- REQ BODY ---")
                print(str(r["req_body"])[:4000])
            print("--- RESP HEADERS ---")
            print(json.dumps(r["resp_headers"], ensure_ascii=False, indent=1))
            print("--- RESP BODY ---")
            print(str(r["resp_body"])[:6000])
        return 0

    _summary(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
