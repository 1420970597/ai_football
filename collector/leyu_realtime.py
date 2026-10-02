#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
乐鱼实时推送消费者 + 盘口走势记录（T2/T3）。

## 协议（实测自 `wss://<host>/yewuws2/push`）

订阅后服务端持续推送，实测 **1.9 秒内收到 4 条盘口更新**，与乐鱼同频。
已解码的指令：

| cmd | 含义 | 关键字段 |
| --- | --- | --- |
| `C0`   | 心跳应答 | `"Heartbeat Reply Success"` |
| `C103` | 比分更新 | `cd.msc`（`S1: 2-1` 序列）、`cd.mpid` |
| `C105` | **盘口赔率更新** | `cd` 为 base64+gzip；`hls2.<chpid>[].ol[]` 含 **`obv`(旧值) / `ov`(新值)** |
| `C102` | 赛事事件/状态 | `cd.cmec`/`cd.mmp`/`cd.mst` || `C109` | 批量赛事结束 | `[{mid, ms:110}]` |
| `C110` | 玩法计数 | `{mid, mc}` |
| `C153` | 链接切换 | `{mid, linkId, hids}` |
| `C303` | 玩法暂停 | `{mid, hpid}` |

## 走势记录的来源

`C105` 的 `ol[]` 每项同时给出 `obv`（变更前）与 `ov`（变更后）。
两者不等即**真实的赔率变动**——这正是「盘口走势」的原始数据，
无需自行比对历史快照。因此本模块直接以推送为源记录走势，
精度与乐鱼一致（推送即记录，不抽样）。

## 设计约束

- collector 层零第三方依赖（AGENTS.md §3.2）：仅用标准库 + 本包内实现。
- 推送线程与 API 同进程，通过 `RealtimeHub` 单例共享状态；
  对外只读，避免把 socket 暴露给 service/api 层。
"""

from __future__ import annotations

import base64
import gzip
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Sequence, Tuple

from .leyu_client import OV_SCALE
from .leyu_ws import LeYuFeed

__all__ = [
    "PriceTick",
    "_MarketState",
    "TrendSeries",
    "RealtimeStats",
    "RealtimeHub",
    "decode_push_payload",
    "parse_c105",
    "parse_c103",
]

#: 每场赛事保留的最大走势点数（防长跑比赛内存无界增长）
MAX_TICKS_PER_MARKET = 400

#: 每场赛事保留的最大事件数
MAX_EVENTS_PER_MATCH = 120


def decode_push_payload(cd: Any) -> Any:
    """解码推送载荷：`base64(gzip(JSON))` 直通明文。

    `C105` 的 `cd` 实测是 base64+gzip 字符串；`C103`/`C102` 等
    直接就是 dict/list。这里统一处理，解不开则原样返回。
    """
    if not isinstance(cd, str) or not cd.startswith("H4sI"):
        return cd
    try:
        return json.loads(gzip.decompress(base64.b64decode(cd)))
    except (ValueError, TypeError, OSError):
        return cd


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #

@dataclass
class _MarketState:
    """某盘口的观测状态：上次赔率 + 是否曾经变动。"""

    __slots__ = ("last", "seen")

    def __init__(self) -> None:
        self.last: Dict[str, float] = {}
        self.seen = 0


@dataclass(frozen=True)
class PriceTick:
    """一次**真实**赔率变动。

    注意 `C105` 实测是**周期性全量快照**（`obv` 恒等于 `ov`，校验 7838/7838
    条均为相等），因此不能把每条推送都当作变动——那会把真实信号稀释到
    千分之一、并使 rate_per_min 完全失真。

    本对象的 `old_ov` 是**上次观测到的值**（由 Hub 自行维护），`new_ov` 是
    本次推送值；两者不等才产生 tick。
    """

    mid: str
    chpid: str
    hid: str          # 盘口实例 ID
    hv: str           # 盘口线
    oid: str          # 选项 ID
    ot: str           # 选项类型（1/2/X/Over/Under）
    old_ov: float     # 上次观测到的赔率（十进制）
    new_ov: float     # 本次赔率（十进制）
    ts_ms: int        # 上游时间戳（毫秒）
    upstream_obv: float = 0.0   # 上游给出的 obv（用于交叉校验）

    @property
    def delta(self) -> float:
        """赔率变化量（正=升赔，负=降赔）。"""
        return self.new_ov - self.old_ov

    @property
    def delta_pct(self) -> float:
        """相对变化百分比。"""
        return (self.delta / self.old_ov * 100.0) if self.old_ov else 0.0

    @property
    def direction(self) -> str:
        """变动方向：up / down / flat。

        语义（赔率视角）：降赔 = 市场认为该结果概率上升 = 资金流入该选项。
        """
        if self.new_ov > self.old_ov:
            return "up"
        if self.new_ov < self.old_ov:
            return "down"
        return "flat"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "mid": self.mid, "chpid": self.chpid, "hv": self.hv,
            "oid": self.oid, "ot": self.ot,
            "old": round(self.old_ov, 4), "new": round(self.new_ov, 4),
            "delta": round(self.delta, 4),
            "delta_pct": round(self.delta_pct, 3),
            "direction": self.direction, "ts": self.ts_ms,
        }


@dataclass
class TrendSeries:
    """某个 (赛事, 盘口) 的赔率走势。"""

    mid: str
    chpid: str
    hv: str = ""
    ticks: Deque[PriceTick] = field(default_factory=lambda: deque(maxlen=MAX_TICKS_PER_MARKET))

    def add(self, tick: PriceTick) -> None:
        self.ticks.append(tick)

    @property
    def n(self) -> int:
        return len(self.ticks)

    def summary(self) -> Dict[str, Any]:
        """走势摘要：次数、方向分布、净变动、速率。"""
        if not self.ticks:
            return {"mid": self.mid, "chpid": self.chpid, "hv": self.hv, "n": 0}
        ups = sum(1 for t in self.ticks if t.direction == "up")
        downs = sum(1 for t in self.ticks if t.direction == "down")
        first = self.ticks[0]
        # 跨度至少按 1 秒计，否则“同一毫秒内的两次变动”会算出
        # 天文数字的 rate_per_min（实测出现过 60000.0），失去意义。
        span_s = max(1.0, (self.ticks[-1].ts_ms - first.ts_ms) / 1000.0)
        return {
            "mid": self.mid, "chpid": self.chpid, "hv": self.hv,
            "n": len(self.ticks),
            "up": ups, "down": downs,
            "span_s": round(span_s, 1),
            # 每分钟变动次数：衡量盘口活跃度
            "rate_per_min": round(len(self.ticks) / span_s * 60.0, 2),
            "last": self.ticks[-1].as_dict(),
            "recent": [t.as_dict() for t in list(self.ticks)[-12:]],
        }


@dataclass
class RealtimeStats:
    """推送连接与消费统计。"""

    connected: int = 0
    reconnects: int = 0
    messages: int = 0
    price_ticks: int = 0
    score_updates: int = 0
    status_updates: int = 0
    finished: int = 0
    started_at: float = field(default_factory=time.time)
    last_message_at: float = 0.0
    last_error: str = ""
    subscribed: int = 0

    def as_dict(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "connected": self.connected,
            "reconnects": self.reconnects,
            "messages": self.messages,
            "price_ticks": self.price_ticks,
            "score_updates": self.score_updates,
            "status_updates": self.status_updates,
            "finished": self.finished,
            "subscribed": self.subscribed,
            "uptime_s": round(now - self.started_at, 1),
            "idle_s": round(now - self.last_message_at, 1) if self.last_message_at else None,
            "last_error": self.last_error,
        }


def parse_c105(decoded: Mapping[str, Any]) -> List[PriceTick]:
    """解析 `C105`（赔率快照）为 PriceTick 列表（**快照级**）。

    结构（实测）::

        {"mid":"5721750", "ms":1, "time":"1790923618643",
         "hls2": {"1": [{"chpid":"1","hid":"...","hv":"","ol":[
              {"oid":"...","ot":"1","ov":"168000","obv":"168000"}, ...]}]}}

    ⚠️ 实测 `obv` **恒等于** `ov`（7838/7838 条均为相等），说明这是周期性的
    全量快照而非增量推送。因此本函数只负责把载荷翻译成“当前值”，
    **是否构成变动由调用方（Hub）对比上次观测值后判定**。
    """
    mid = str(decoded.get("mid", ""))
    ts = int(str(decoded.get("time") or decoded.get("t") or 0).strip() or 0)
    ticks: List[PriceTick] = []

    def walk_block(block: Mapping[str, Any], chpid_hint: str = "") -> None:
        chpid = str(block.get("chpid") or chpid_hint)
        hv = str(block.get("hv") or "")
        hid = str(block.get("hid") or "")
        bts = int(str(block.get("t") or ts).strip() or ts)
        for entry in (block.get("ol") or ()):
            if not isinstance(entry, Mapping):
                continue
            try:
                new_raw = int(str(entry.get("ov")))
            except (TypeError, ValueError):
                continue
            if new_raw <= 0:
                continue
            obv_raw = entry.get("obv")
            try:
                obv_int = int(str(obv_raw)) if obv_raw is not None else new_raw
            except (TypeError, ValueError):
                obv_int = new_raw
            new_val = new_raw / OV_SCALE
            ticks.append(PriceTick(
                mid=mid, chpid=chpid, hid=hid, hv=hv,
                oid=str(entry.get("oid", "")), ot=str(entry.get("ot", "")),
                # 先填为“与自身相等”；真正的前值由 Hub 维护后替换
                old_ov=new_val, new_ov=new_val,
                ts_ms=bts or ts,
                upstream_obv=(obv_int / OV_SCALE) if obv_int > 0 else new_val,
            ))

    hls = decoded.get("hls2") or decoded.get("hls") or {}
    if isinstance(hls, Mapping):
        for chpid_key, blocks in hls.items():
            if isinstance(blocks, Mapping):
                walk_block(blocks, str(chpid_key))
            elif isinstance(blocks, Sequence):
                for b in blocks:
                    if isinstance(b, Mapping):
                        walk_block(b, str(chpid_key))
    elif isinstance(hls, Sequence):
        for b in hls:
            if isinstance(b, Mapping):
                walk_block(b)
    return ticks


def parse_c103(decoded: Mapping[str, Any]) -> Optional[Tuple[str, Tuple[int, int]]]:
    """解析 `C103`（比分）→ `(mid, (主队比分, 客队比分))`；无 `S1` 返回 None。"""
    mid = str(decoded.get("mid", ""))
    for chunk in (decoded.get("msc") or ()):
        text = str(chunk).strip().strip("'")
        if not text.startswith("S1|"):
            continue
        _, _, val = text.partition("|")
        left, _, right = val.partition(":")
        try:
            return mid, (int(left), int(right))
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------- #
# Hub：后台推送消费 + 共享只读状态
# --------------------------------------------------------------------------- #

class RealtimeHub:
    """后台常驻的实时推送消费者。

    用法::

        hub = RealtimeHub(client_factory)   # client_factory → (ws_url, request_id, origin)
        hub.start()
        hub.snapshot(mid)                   # 读某场走势
        hub.stop()

    线程模型：单个后台 daemon 线程负责连接、订阅、消费与重连；
    其它线程只读共享状态（内部加锁）。
    """

    def __init__(
        self,
        session_provider: Any,
        mids_provider: Optional[Callable[[], Sequence[str]]] = None,
        subscribe_interval_s: float = 45.0,
        max_matches: int = 60,
    ) -> None:
        """
        Args:
            session_provider: 提供 `Session`（含 request_id / host / origin）。
            mids_provider: 返回要订阅的赛事 ID 列表；定期刷新。
            subscribe_interval_s: 重新计算订阅列表的间隔。
            max_matches: 单次订阅上限（订阅过多会被上游限制）。
        """
        self.session_provider = session_provider
        self.mids_provider = mids_provider
        self.subscribe_interval_s = subscribe_interval_s
        self.max_matches = max_matches

        self.stats = RealtimeStats()
        self._lock = threading.RLock()
        self._trends: Dict[Tuple[str, str, str], TrendSeries] = {}
        # 上次观测到的赔率（key = mid|chpid|hv|oid）→ 仅当变化时才记 tick
        self._last_price: Dict[Tuple[str, str, str, str], float] = {}
        self._snapshots = 0
        self._scores: Dict[str, Tuple[int, int]] = {}
        self._status: Dict[str, Dict[str, Any]] = {}
        self._events: Dict[str, Deque[Dict[str, Any]]] = {}
        self._finished: set = set()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._subscribed: List[str] = []

    # -- 生命周期 -----------------------------------------------------------

    def start(self) -> bool:
        """启动后台线程（幂等）。返回是否实际启动。"""
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="leyu-realtime",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- 读取（只读，供 service/api 使用） ---------------------------------

    def trend(self, mid: str, chpid: str = "", hv: str = "") -> Dict[str, Any]:
        """某场某个盘口的走势摘要；不指定 chpid 时返回该场全部盘口。"""
        with self._lock:
            items = [s for (m, c, h), s in self._trends.items() if m == mid
                     and (not chpid or c == chpid)
                     and (not hv or h == hv)]
            if chpid and hv:
                key = (mid, chpid, hv)
                s = self._trends.get(key)
                return s.summary() if s else {"mid": mid, "chpid": chpid,
                                              "hv": hv, "n": 0}
            return {
                "mid": mid,
                "n_markets": len(items),
                "markets": [s.summary() for s in items],
            }

    def score(self, mid: str) -> Optional[Tuple[int, int]]:
        with self._lock:
            return self._scores.get(mid)

    def status(self, mid: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._status.get(mid) or {})

    def events(self, mid: str) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._events.get(mid) or ())

    def is_finished(self, mid: str) -> bool:
        with self._lock:
            return mid in self._finished

    def subscribed(self) -> List[str]:
        with self._lock:
            return list(self._subscribed)

    def health(self) -> Dict[str, Any]:
        with self._lock:
            baseline = len(self._last_price)
            snaps = self._snapshots
        return {
            "running": self.running,
            # 已建立基线的赔率选项数：对比它可判断推送是否在正常覆盖盘口
            "price_options": baseline,
            # 收到的赔率快照条数（含未变化的），用于区分“推送正常但没变化”
            "price_snapshots": snaps,
            "changed_ratio": round(self.stats.price_ticks / snaps, 4) if snaps else 0.0,
            **self.stats.as_dict(),
        }

    # -- 内部 ---------------------------------------------------------------

    def _record_ticks(self, ticks: Sequence[PriceTick]) -> None:
        """记录赔率变动。

        关键：`C105` 是周期全量快照（`obv` 恒等于 `ov`），所以必须用
        **自行维护的上次值** 对比，只把真正变化的项记为走势。
        否则每条快照都入库，真实信号会被稀释上千倍。
        """
        changed = 0
        with self._lock:
            for t in ticks:
                key = (t.mid, t.chpid, t.hv, t.oid)
                prev = self._last_price.get(key)
                self._last_price[key] = t.new_ov
                if prev is None:
                    # 首次观测到该选项：建立基线，不算变动
                    continue
                if abs(prev - t.new_ov) < 1e-9:
                    continue
                real = PriceTick(
                    mid=t.mid, chpid=t.chpid, hid=t.hid, hv=t.hv,
                    oid=t.oid, ot=t.ot, old_ov=prev, new_ov=t.new_ov,
                    ts_ms=t.ts_ms, upstream_obv=t.upstream_obv,
                )
                mkey = (real.mid, real.chpid, real.hv)
                series = self._trends.get(mkey)
                if series is None:
                    series = TrendSeries(mid=real.mid, chpid=real.chpid, hv=real.hv)
                    self._trends[mkey] = series
                series.add(real)
                changed += 1
            self._snapshots += len(ticks)
            self.stats.price_ticks += changed

    def _market_states(self) -> int:
        """已建立基线的选项数（用于诊断）。"""
        with self._lock:
            return len(self._last_price)

    def _record_event(self, mid: str, event: Mapping[str, Any]) -> None:
        with self._lock:
            dq = self._events.get(mid)
            if dq is None:
                dq = deque(maxlen=MAX_EVENTS_PER_MATCH)
                self._events[mid] = dq
            dq.append(dict(event))

    def _handle_message(self, msg: Mapping[str, Any]) -> None:
        cmd = str(msg.get("cmd") or "")
        self.stats.messages += 1
        self.stats.last_message_at = time.time()

        if cmd in ("C105", "C2", "C21"):
            decoded = decode_push_payload(msg.get("cd"))
            if isinstance(decoded, Mapping):
                ticks = parse_c105(decoded)
                if ticks:
                    self._record_ticks(ticks)
            return

        if cmd in ("C103", "C1021"):
            decoded = decode_push_payload(msg.get("cd"))
            if isinstance(decoded, Mapping):
                parsed = parse_c103(decoded)
                if parsed:
                    mid, score = parsed
                    with self._lock:
                        self._scores[mid] = score
                    self.stats.score_updates += 1
                self._record_event(str(decoded.get("mid", "")),
                                   {"cmd": cmd, "cmec": decoded.get("cmec"),
                                    "mmp": decoded.get("mmp"), "mst": decoded.get("mst")})
            return

        if cmd == "C102":
            decoded = decode_push_payload(msg.get("cd"))
            if isinstance(decoded, Mapping):
                mid = str(decoded.get("mid", ""))
                with self._lock:
                    self._status[mid] = {
                        "cmec": decoded.get("cmec"),
                        "mmp": decoded.get("mmp"),
                        "mst": decoded.get("mst"),
                        "ha": decoded.get("ha"),
                    }
                self.stats.status_updates += 1
                self._record_event(mid, {"cmd": cmd, "cmec": decoded.get("cmec")})
            return

        if cmd == "C109":
            decoded = decode_push_payload(msg.get("cd"))
            items = decoded if isinstance(decoded, Sequence) else ()
            with self._lock:
                for it in items:
                    if isinstance(it, Mapping):
                        self._finished.add(str(it.get("mid", "")))
            self.stats.finished += len(items)
            return

        if cmd == "C110":
            decoded = decode_push_payload(msg.get("cd"))
            if isinstance(decoded, Mapping):
                self._record_event(str(decoded.get("mid", "")),
                                   {"cmd": cmd, "mc": decoded.get("mc")})
            return

        if cmd == "C303":
            decoded = decode_push_payload(msg.get("cd"))
            if isinstance(decoded, Mapping):
                mid = str(decoded.get("mid", ""))
                self._status.setdefault(mid, {})["suspended_hpid"] = decoded.get("hpid")
                self._record_event(mid, {"cmd": cmd, "hpid": decoded.get("hpid")})
            return

    def _pick_mids(self) -> List[str]:
        if self.mids_provider is None:
            return []
        try:
            mids = [str(m) for m in (self.mids_provider() or ())]
        except Exception as exc:  # noqa: BLE001 - 订阅来源失败不应终止推送线程
            self.stats.last_error = "mids_provider 失败: %s" % exc
            return []
        # 已结束的不再订阅
        with self._lock:
            mids = [m for m in mids if m not in self._finished]
        return mids[: self.max_matches]

    def _run(self) -> None:
        """推送主循环：连接 → 订阅 → 消费 → 断线重连。"""
        while not self._stop.is_set():
            session = None
            try:
                session = self.session_provider.acquire()
            except Exception as exc:  # noqa: BLE001 - 会话失败应退避重试
                self.stats.last_error = "会话获取失败: %s" % exc
                if self._stop.wait(10.0):
                    return
                continue

            host = (session.host or "").rstrip("/")
            if not host:
                self.stats.last_error = "会话缺少 host"
                if self._stop.wait(10.0):
                    return
                continue
            scheme = "wss" if host.startswith("https") else "ws"
            ws_url = "%s://%s/yewuws2/push?requestId=%s" % (
                scheme, host.split("://", 1)[-1], session.request_id)

            feed = LeYuFeed(ws_url, session.request_id,
                            session.origin or "", timeout=25.0)
            try:
                feed.connect()
                self.stats.connected += 1
                mids = self._pick_mids()
                if mids:
                    feed.subscribe_odds(mids)
                    with self._lock:
                        self._subscribed = mids
                    self.stats.subscribed = len(mids)

                last_sub = time.time()
                while not self._stop.is_set():
                    msg = feed.recv()
                    if msg is not None:
                        self._handle_message(msg)
                    # 定期刷新订阅列表（赛事会陆续开始/结束）
                    if time.time() - last_sub >= self.subscribe_interval_s:
                        new_mids = self._pick_mids()
                        if new_mids and new_mids != self._subscribed:
                            feed.subscribe_odds(new_mids)
                            with self._lock:
                                self._subscribed = new_mids
                            self.stats.subscribed = len(new_mids)
                        last_sub = time.time()
            except Exception as exc:  # noqa: BLE001 - 任何异常都要重连而不是退出
                self.stats.last_error = "%s: %s" % (type(exc).__name__, exc)
            finally:
                try:
                    feed.close()
                except Exception as exc:  # noqa: BLE001 - 关闭失败不应阻止重连
                    self.stats.last_error = "close 失败: %s" % exc

            if self._stop.is_set():
                return
            self.stats.reconnects += 1
            if self._stop.wait(3.0):
                return
