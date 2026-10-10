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
| `C102` | 赛事事件/状态 | `cd.cmec`/`cd.mmp`/`cd.mst` || `C109` | 批量赛事结束 | `[{mid, ms:3}]` |
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
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Sequence, Tuple

from .leyu_client import OV_SCALE
from .leyu_ws import LEYUFeed

#: 实时表落盘节流（秒）。
#:
#: 推送频率极高（实测一小时 16 万次赔率更新），逐条写盘会打满磁盘 IO；
#: 取 5s 是一个兼顾「重启后数据足够新」与「写盘开销可忽略」的折中。
_LIVE_SAVE_INTERVAL_S = 5.0

#: 比分落盘节流（秒）。
#:
#: 比分推送比赔率稀疏得多（实测数小时 300+ 条），但结算需要的是
#: **终场比分**，因此取更短的 3s —— 让“比赛刚结束”的比分尽快落盘，
#: 以免重启后丢掉那场唯一的赛果来源（详见 `ScoreStore`）。
_SCORE_SAVE_INTERVAL_S = 3.0


def _live_snapshot_path(trend_root: Optional[str]) -> Optional[str]:
    """由走势目录推出实时表落盘路径（与 `_trends` 同级）。

    返回值 None 表示不落盘（未配置目录时，退化为纯内存表）。
    """
    if not trend_root:
        return None
    try:
        return str(Path(trend_root).parent / "_live" / "live.json")
    except (TypeError, ValueError):
        return None


def _score_snapshot_path(trend_root: Optional[str]) -> Optional[str]:
    """比分落盘路径（与 `_live`、`_trends` 同级）。

    与实时表分开存：实时表是“现在多少钱”（秒级、体量大、可丢），
    比分是“最终赛果”（稀少、**不可再生**）—— 冗余丢一次就永远补不回，
    因此单文件独立保存（见 `ScoreStore`）。
    """
    if not trend_root:
        return None
    try:
        return str(Path(trend_root).parent / "_live" / "scores.json")
    except (TypeError, ValueError):
        return None

__all__ = [
    "PriceTick",
    "TrendSeries",
    "TrendStore",
    "LiveBook",
    "LiveQuote",
    "ScoreStore",
    "RealtimeStats",
    "RealtimeHub",
    "decode_push_payload",
    "parse_c105",
    "parse_c103",
]

#: 每场赛事保留的最大走势点数（防长跑比赛内存无界增长）
MAX_TICKS_PER_MARKET = 400

#: 走势落盘目录（相对快照根）
TREND_SUBDIR = "_trends"

#: 落盘批大小：累积这么多 tick 后刷一次盘
TREND_FLUSH_EVERY = 25

#: 落盘间隔上限（秒）：即使不足批大小也要写，保证不丢数据
TREND_FLUSH_INTERVAL_S = 20.0

#: 单场走势文件轮转阈值（MB）。
#: 实测：高频推送下单个活跃赛事的走势文件几小时就到 10MB，
#: 而原阈值 64MB × 百来场 → 磁盘可达数 GB，属**无界增长**。
#: 现改为 4MB，并把旧文件滚动为 `.1`（只保留最近两代）。
TREND_FILE_MAX_MB = 4.0

#: 走势目录总量上限（MB）。超过时删除最旧的文件。
#: 内存侧已有 maxlen 限流，但落盘必须同样有硬上限。
TREND_DIR_MAX_MB = 256.0

#: 每场赛事保留的最大事件数
MAX_EVENTS_PER_MATCH = 120

#: 行情新鲜度上限（秒）。**超过此年龄的赔率不得再被当作「正在进行中」**。
#:
#: 为何必须有这个上限（本项目真实故障）：`LiveBook.save()/load()` 会把
#: 内存实时表落盘再回填，而回填时早期实现把每行的 `at` 一律写成「载入那
#: 一刻」，于是 **十几小时前的旧行情在健康检查里显示为「75 秒前」**，
#: `live_mids()` 也把它们全部算作活跃场次 —— 实测重启后 `/api/v1/analysis`
#: 声称 `count=527, source=push`（「真实进行中」），而当日实际滚球数只有
#: 几十场。这正是 HANDOVER §6.3 记录过的「用快照时效冒充进行中」的同一类
#: 错误，只是换了一条路径（持久化回填）重新长出来。
#:
#: 取值依据：`C105` 是**周期性全量快照**（实测一小时可达 16 万次 tick），
#: 真正在滚球的场次不会连续 15 分钟没有任何新价。900s 与赛程 `grace_s`
#: 同量级，宁可宽松也**不要误杀**低级别赛事（它们推送频率本就低）。
DEFAULT_QUOTE_MAX_AGE_S = 900.0


def _to_int(value: object, default: int = 0) -> int:
    """容错整数转换（配置可能来自环境变量的字符串）。"""
    try:
        return int(str(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _to_float(value: object, default: float = 0.0) -> float:
    """容错浮点转换（非数值/非有限值均回退 default）。

    为何必需：实时表写入跑在**推送热路径**上（`C105` 每小时可达 16 万条），
    而推送载荷来自上游、不可信（可能是字符串/None/异常值）。
    直接 `float()` 会抛 `ValueError` 并冒到推送循环 → 被外层 `except`
    当成链路故障而**触发不必要的重连**（丢消息）。宁可丢一个脏值。
    """
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


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
    order_detail: Mapping[str, Any] = field(default_factory=dict)

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
class LiveQuote:
    """某个可投注选项的**最新**赔率（内存实时表的一行）。

    与 `PriceTick` 的区别：`PriceTick` 描述「一次变动」（用于走势），
    而本对象描述「当前值」—— 看板与决策真正需要的后者。

    为何要单独维护：全量快照库（`output/snapshots`）是**不可变历史**，
    一场比赛同一盘口会累积几十份快照；直接查库既慢（冷启动实测 45s）
    又会挑到过期价格（实测拿 1.8 小时前的 3.32 去算一个已结算的盘口）。
    推送消息里本来就有**当前值**（`C105` 是周期性全量快照），
    因此在 Hub 里存一份内存实时表，查询便退化为字典查找。
    """

    mid: str
    chpid: str
    hv: str
    oid: str
    ot: str
    odds: float
    ts_ms: int
    #: 本机写入时刻（单调时钟不可回拨）；用于算“这个价多旧了”
    at: float = 0.0
    order_detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def age_s(self) -> float:
        return max(0.0, time.monotonic() - self.at) if self.at else 0.0

    @property
    def quote_age_s(self) -> float:
        """行情**本身**的年龄（秒），按上游 `ts_ms` 计算。

        与 `age_s` 的关键区别：`age_s` 衡量「本进程多久前写过这一行」，
        会因落盘/回填而被重置；`quote_age_s` 直接对上游时间戳求差，
        因此**重启回填的历史行情也会如实显示为很旧**，无法被伪装成新鲜。

        Returns:
            年龄（秒）；`ts_ms` 缺失（<=0）时返回 `inf` —— 即「无法证明
            它新鲜」，调用方据此不得把它算作活跃行情。
        """
        if self.ts_ms <= 0:
            return math.inf
        # 容错转换：`ts_ms` 类型未约束（可能来自落盘 JSON 或推送脏字段），
        # 直接算数会抛错并冒泡到调用方（决策/看板循环不允许因单个脏字段挂掉）。
        ts_f = _to_float(self.ts_ms, 0.0)
        if ts_f <= 0:
            return math.inf
        return max(0.0, time.time() - ts_f / 1000.0)


class LiveBook:
    """内存实时赔率表：`(mid, chpid, hv, oid) -> LiveQuote`。

    ## 为何这是本项目最重要的性能修复

    用户要求：「系统应当以 leyu 的 api 更新频率做数据采集，存储至本地，
    查询的时候读取本地异步数据」。

    推送链路本身就是「以乐鱼频率更新」的：实测 `C105` 是**周期性全量
    快照**（`price_ticks` 一小时可达 16 万次），每条都带着每个选项的
    **当前赔率**。以前这些值只被用来算「变动」（走势），算完就丢了；
    查询时再去翻磁盘上的不可变历史（6.5 万个文件、一场 216 条快照），
    既慢又容易取到过期价。

    本类把推送里的当前值直接留存在内存：
      * 写入：`_record_ticks` 时顺带 upsert（唯一写入点，不会漏）；
      * 读取：`book(mid)` 返回该场每个盘口的当前赔率，O(盘口数)。

    文件落盘不在这里做：`TrendStore` 已按场分文件持久化走势，
    快照库仍由采集链路按盘口落库（供建模/回放）。本类只负责
    「查询路径不再碰磁盘」。
    """

    def __init__(self, snapshot_path: Optional[str] = None) -> None:
        self._rows: Dict[Tuple[str, str, str, str], LiveQuote] = {}
        #: mid -> 该场当前活跃的 key 集合（便于整场读取，避免全表扫描）
        self._by_mid: Dict[str, Dict[Tuple[str, str, str, str], None]] = {}
        self._lock = threading.RLock()
        self.updates = 0
        #: 本地落盘路径（原子写）。重启后立即有数据，**无需重扫快照库**。
        self.snapshot_path: Optional[Path] = (
            Path(snapshot_path) if snapshot_path else None)
        self.last_save_at = 0.0
        self.last_error = ""

    def upsert_many(self, ticks: Sequence[PriceTick]) -> None:
        """写入/更新一批赔率（来自推送快照，**含未变动的项**）。"""
        if not ticks:
            return
        now = time.monotonic()
        with self._lock:
            for t in ticks:
                key = (t.mid, t.chpid, t.hv, t.oid)
                # 容错：脏赔率（非数值）不得让整批推送中断。
                odds = _to_float(t.new_ov, 0.0)
                if odds <= 1.0:
                    continue        # 非法赔率不入表（1.0 以下不可能成交）
                previous = self._rows.get(key)
                if previous and previous.ts_ms > _to_int(t.ts_ms, 0):
                    continue  # REST enrichment must not roll back newer push quotes.
                self._rows[key] = LiveQuote(
                    mid=t.mid, chpid=t.chpid, hv=t.hv, oid=t.oid, ot=t.ot,
                    odds=odds, ts_ms=_to_int(t.ts_ms, 0), at=now,
                    order_detail=dict(t.order_detail))
                self._by_mid.setdefault(t.mid, {})[key] = None
                self.updates += 1

    def drop_match(self, mid: str) -> int:
        """移除某场全部行（比赛结束时调用，防止内存无限增长）。"""
        with self._lock:
            keys = self._by_mid.pop(str(mid), None)
            if not keys:
                return 0
            for k in list(keys):
                self._rows.pop(k, None)
            return len(keys)

    def book(self, mid: str) -> List[LiveQuote]:
        """某场全部当前赔率（按盘口/线/选项排序，输出可复现）。"""
        with self._lock:
            keys = self._by_mid.get(str(mid))
            if not keys:
                return []
            rows = [self._rows[k] for k in keys if k in self._rows]
        rows.sort(key=lambda r: (r.chpid, r.hv, r.ot, r.oid))
        return rows

    def live_mids(self, max_age_s: Optional[float] = None) -> list[str]:
        """当前有行情的赛事 ID。

        Args:
            max_age_s: 只返回「最新一行行情的真实年龄 ≤ 该值」的场次。
                `None`（默认）不设限，保持历史行为（回填即可用）。
                判据用 :attr:`LiveQuote.quote_age_s`（按上游时间戳），
                而不是 `age_s`（按本进程写入时刻）—— 否则重启回填会把
                陈旧行情算成活跃，正是本项目踩过的坑。

        Returns:
            赛事 ID 列表（稳定排序）。
        """
        with self._lock:
            if max_age_s is None:
                return sorted(self._by_mid)
            limit = max(0.0, _to_float(max_age_s, 0.0))
            fresh: list[str] = []
            for mid, keys in self._by_mid.items():
                # 用 `in` 而不是直接下标：两表理论上同步维护，但一旦不一致，
                # KeyError 会让整个实时看板 500 —— 这里只丢一行，不丢整表。
                newest = min((self._rows[k].quote_age_s for k in keys
                              if k in self._rows),
                             default=math.inf)
                if newest <= limit:
                    fresh.append(mid)
            return sorted(fresh)

    def n_matches(self) -> int:
        with self._lock:
            return len(self._by_mid)

    def n_rows(self) -> int:
        with self._lock:
            return len(self._rows)

    def health(self) -> Dict[str, Any]:
        with self._lock:
            newest = max((r.at for r in self._rows.values()), default=0.0)
            oldest = min((r.at for r in self._rows.values()), default=0.0)
            # 行情**真实**年龄（按上游 ts_ms）：与 at 不同，它无法被回填重置，
            # 因此健康检查能如实暴露「数据其实是旧的」。
            quote_newest = min((r.quote_age_s for r in self._rows.values()),
                               default=0.0)
            fresh = sum(1 for r in self._rows.values()
                        if r.quote_age_s <= DEFAULT_QUOTE_MAX_AGE_S)
        return {
            "matches": self.n_matches(),
            "rows": self.n_rows(),
            "updates": self.updates,
            "newest_age_s": (round(time.monotonic() - newest, 1) if newest else None),
            "oldest_age_s": (round(time.monotonic() - oldest, 1) if oldest else None),
            #: 最新一行行情的真实年龄；None 表示无数据或**无法判定**。
            #: 必须过滤非有限值：`Infinity` 不是合法 JSON，严格解析器
            #: （如浏览器 `JSON.parse`）会直接报错。
            "quote_newest_age_s": (round(quote_newest, 1)
                                   if self._rows and math.isfinite(quote_newest)
                                   else None),
            #: 仍在新鲜度上限内的**条目**数（0 = 全部陈旧，页面不得声称进行中）
            "fresh_rows": fresh,
            "fresh_max_age_s": DEFAULT_QUOTE_MAX_AGE_S,
            "persist_path": (str(self.snapshot_path)
                             if self.snapshot_path else ""),
            "last_save_age_s": (round(time.monotonic() - self.last_save_at, 1)
                                if self.last_save_at else None),
            "last_error": self.last_error,
        }

    # -- 本地持久化（用户要求：采集落本地，查询读本地） ---------------------

    def save(self, force: bool = False) -> bool:
        """把当前实时表**原子写入**本地 JSON。

        为何需要：用户明确要求「以 leyu 的 api 更新频率做数据采集，
        存储至本地，查询的时候读取本地异步数据」。内存表解决了一次运行
        期间的查询速度，而落盘则使**重启后立即有数据** —— 不必等第一批
        推送，也不必重扫 6.5 万个历史快照（实测 14~45s）。

        Args:
            force: 忽略节流（启动/退出时用）。

        Returns:
            是否真的写盘。
        """
        path = self.snapshot_path
        if path is None:
            return False
        now = time.monotonic()
        if not force and (now - self.last_save_at) < _LIVE_SAVE_INTERVAL_S:
            return False
        with self._lock:
            # Historical prices are archived separately. The current book must
            # not accumulate obsolete option IDs for days (207k rows observed).
            cutoff_ms = (time.time() - DEFAULT_QUOTE_MAX_AGE_S) * 1000
            stale = [k for k, r in self._rows.items() if r.ts_ms < cutoff_ms]
            for key in stale:
                self._rows.pop(key, None)
                keys = self._by_mid.get(key[0])
                if keys is not None:
                    keys.pop(key, None)
                    if not keys:
                        self._by_mid.pop(key[0], None)
            rows = list(self._rows.values())
        # Quotes are replaced, never mutated; format the frozen references
        # outside the lock so decision snapshots can continue during writes.
        payload = {
            "version": 1,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "rows": [[r.mid, r.chpid, r.hv, r.oid, r.ot,
                      round(_to_float(r.odds, 0.0), 6), _to_int(r.ts_ms, 0), dict(r.order_detail)]
                     for r in rows],
        }
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, path)          # 原子替换，避免读到半写文件
            self.last_save_at = now
            self.last_error = ""
            return True
        except (OSError, TypeError, ValueError) as exc:
            self.last_error = "实时表落盘失败: %s" % exc
            return False

    def load(self) -> int:
        """从本地 JSON 回填实时表（启动时调用）。返回回填行数。

        ## 回填的行带**真实年龄**，不得伪装成新鲜（本项目真实故障）

        早期实现在这里把每行的 `at` 写成「载入这一刻」（`at=now`），后果：

          * `health().newest_age_s` 把 24 小时前的行情报成「75 秒前」；
          * `live_mids()` 把全部回填场次算作活跃 → `_live_ids_from_push()`
            返回 527 场 → `/api/v1/analysis` 声称 `count=527, source=push`，
            而当日真实滚球只有几十场。

        这正是 HANDOVER §6.3 的「用快照时效冒充进行中」，只是换了一条路径
        （持久化回填）重新长出来。修法：`at` 按行情的 `ts_ms` 反推，
        使 `age_s` 与真实年龄一致；同时保留 `quote_age_s` 作为不依赖
        单进程写入时刻的第二重口径。

        保留 `load()` 的初衷不变 —— 重启后**立即有数据**（免重扫 10 万快照），
        只是这些数据的陈旧程度现在能被看见，由调用方（如
        :meth:`live_mids` 的 `max_age_s`）决定要不要用。
        """
        path = self.snapshot_path
        if path is None or not path.exists():
            return 0
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as exc:
            self.last_error = "实时表读取失败: %s" % exc
            return 0
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return 0
        now = time.monotonic()
        now_ms = time.time() * 1000.0
        n = 0
        with self._lock:
            for item in rows:
                if not isinstance(item, (list, tuple)) or len(item) < 7:
                    continue
                mid, chpid, hv, oid, ot, odds, ts_ms = item[:7]
                try:
                    f_odds = float(odds)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(f_odds) or f_odds <= 1.0:
                    continue
                i_ts = _to_int(ts_ms, 0)
                if i_ts < now_ms - DEFAULT_QUOTE_MAX_AGE_S * 1000:
                    continue  # Historical archives, not the current book, retain expired quotes.
                # 反推写入时刻：让 age_s 等于行情的**真实**年龄。
                # ts_ms 缺失（<=0）时无从判断 → 视为很旧（保持诚实，
                # 避免把无法证明新鲜的行情算作活跃）。
                if i_ts > 0:
                    at = now - max(0.0, (now_ms - i_ts) / 1000.0)
                else:
                    at = now - (DEFAULT_QUOTE_MAX_AGE_S * 2.0)
                key = (str(mid), str(chpid), str(hv), str(oid))
                self._rows[key] = LiveQuote(
                    mid=str(mid), chpid=str(chpid), hv=str(hv), oid=str(oid),
                    ot=str(ot), odds=f_odds, ts_ms=i_ts, at=at,
                    order_detail=dict(item[7]) if len(item) > 7 and isinstance(item[7], Mapping) else {})
                self._by_mid.setdefault(str(mid), {})[key] = None
                n += 1
        return n


class ScoreStore:
    """比分与结束状态的**本地持久化**（结算的赛果来源）。

    ## 为何必须有它（用户问题 2「正确率没有统计」的真正根因）

    实测两条上游路径都拿不到**历史**比分：

      * `getOriginalDataPB`（赛程）—— 根本不返回 `msc`；
      * `structureMatchBaseInfoByMidsPB`（盘口）—— 直播中的场次带 `msc`，
        但**已结束的场次 `score_raw` 恒为空串**（实测 11/11 全空）。

    即：赛果只在「比赛进行中」那个时间窗内可得，一旦结束就再也拿不到。
    而结算总是发生在比赛结束**之后** → 永远 `graded=0`
    → 命中率/ROI/CLV 永远算不出来。

    修法：进程在接收推送时（`C103` 比分 / `C109` 结束）就把它们落盘。
    这样即使比赛早已结束、上游已不提供，本地仍有终场比分可用于结算。

    ⚠️ 只存比分与是否结束，**不存投注相关隐私**；文件在 output 卷内。
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path) if path else None
        self.last_save_at = 0.0
        self.last_error = ""
        self._lock = threading.RLock()

    def save(self, scores: Mapping[str, Any], finished: Any,
             force: bool = False, half_scores: Optional[Mapping[str, Any]] = None,
             final_proofs: Optional[Mapping[str, Any]] = None) -> bool:
        """原子写入 `{mid: {"ft": [h,a], "ht": [h,a]|null, "done": bool}}`。"""
        path = self.path
        if path is None:
            return False
        now = time.monotonic()
        if not force and (now - self.last_save_at) < _SCORE_SAVE_INTERVAL_S:
            return False
        done = {str(m) for m in (finished or ())}
        payload: Dict[str, Any] = {"version": 2}
        try:
            payload["saved_at"] = datetime.now(timezone.utc).isoformat()
            payload["scores"] = {
                str(mid): {"ft": [int(v[0]), int(v[1])],
                           "done": str(mid) in done, "ht": (half_scores or {}).get(str(mid)),
                           "final_proof": (final_proofs or {}).get(str(mid))}
                for mid, v in (scores or {}).items()
                if v and v[0] is not None and v[1] is not None}
        except (TypeError, ValueError, IndexError):
            return False
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, path)
            self.last_save_at = now
            self.last_error = ""
            return True
        except (OSError, TypeError, ValueError) as exc:
            self.last_error = "比分落盘失败: %s" % exc
            return False

    def load(self) -> Dict[str, Any]:
        """读回 `{mid: {"ft": [...], "done": bool}}`；缺失/损坏返回空。"""
        path = self.path
        if path is None or not path.exists():
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as exc:
            self.last_error = "比分读取失败: %s" % exc
            return {}
        rows = payload.get("scores") if isinstance(payload, dict) else None
        return dict(rows) if isinstance(rows, dict) else {}


class TrendStore:
    """走势持久化：JSONL 追写，进程重启后可恢复。

    为何需要：走势原本只在内存（`RealtimeHub._trends`），
    容器重启即全部丢失，历史无法回溯、也无法供后续分析与回测使用。

    设计：
    * 每场一个文件 `<_trends>/<mid>.jsonl`，**追写**（append）——
      不重写整个文件，避免高频写入时的写放大。
    * 先写缓冲，达到 `flush_every` 条或超过 `flush_interval_s` 时刷盘，
      避免每条都 fsync 拖慢推送消费。
    * 写入失败只告警不抛错——采集不能因为磁盘问题而中断。
    """

    def __init__(
        self,
        root: Optional[str] = None,
        flush_every: int = TREND_FLUSH_EVERY,
        flush_interval_s: float = TREND_FLUSH_INTERVAL_S,
        max_file_mb: float = TREND_FILE_MAX_MB,
        max_dir_mb: float = TREND_DIR_MAX_MB,
    ) -> None:
        self.root = Path(root) if root else None
        # 以下参数可能来自环境变量（字符串），非法值一律回退默认，
        # 构造 TrendStore 不应因为一个坏配置就抛异常。
        try:
            self.flush_every = max(1, int(str(flush_every)))
        except (TypeError, ValueError, OverflowError):
            self.flush_every = TREND_FLUSH_EVERY
        try:
            self.flush_interval_s = float(str(flush_interval_s))
        except (TypeError, ValueError, OverflowError):
            self.flush_interval_s = TREND_FLUSH_INTERVAL_S
        try:
            mb = float(str(max_file_mb))
            self.max_file_bytes = int(mb * 1024 * 1024) if mb > 0 else int(
                TREND_FILE_MAX_MB * 1024 * 1024)
        except (TypeError, ValueError, OverflowError):
            self.max_file_bytes = int(TREND_FILE_MAX_MB * 1024 * 1024)
        try:
            mb = float(str(max_dir_mb))
            self.max_dir_bytes = int(mb * 1024 * 1024) if mb > 0 else int(
                TREND_DIR_MAX_MB * 1024 * 1024)
        except (TypeError, ValueError, OverflowError):
            self.max_dir_bytes = int(TREND_DIR_MAX_MB * 1024 * 1024)
        self._buf: Dict[str, List[str]] = {}
        self._last_flush = time.time()
        self._lock = threading.RLock()
        self.written = 0
        self.dropped = 0
        self.rotated = 0
        self.pruned = 0
        self.last_error = ""
        self._last_prune = 0.0
        if self.root:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.root = None
                self.last_error = "创建走势目录失败: %s" % exc

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def _path(self, mid: str) -> Path:
        """该场赛事的走势文件路径。

        `mid` 来自上游，做字符白名单过滤以防路径穿越（如 `../`）。
        """
        assert self.root is not None
        safe = "".join(ch for ch in str(mid) if ch.isalnum() or ch in "-_")
        return self.root / ("%s.jsonl" % (safe or "unknown"))

    def append_many(self, ticks: Sequence[PriceTick]) -> None:
        """缓冲一批 tick；按批/按时间自动刷盘。"""
        if not self.enabled or not ticks:
            return
        with self._lock:
            for t in ticks:
                self._buf.setdefault(t.mid, []).append(
                    json.dumps(t.as_dict(), ensure_ascii=False, separators=(",", ":")))
            total = sum(len(v) for v in self._buf.values())
            due = (total >= self.flush_every
                   or (time.time() - self._last_flush) >= self.flush_interval_s)
        if due:
            self.flush()

    def flush(self) -> int:
        """把缓冲区写入磁盘。返回写入条数（失败不抛错）。"""
        if not self.enabled:
            return 0
        with self._lock:
            buf, self._buf = self._buf, {}
            self._last_flush = time.time()
        n = 0
        for mid, lines in buf.items():
            path = self._path(mid)
            try:
                if path.exists() and path.stat().st_size > self.max_file_bytes:
                    # 单场文件过大时轮转，避免单个文件无限增大。
                    # 只保留最近两代（.1 会被覆盖），所以单场磁盘占用有上界。
                    try:
                        path.replace(path.with_suffix(".jsonl.1"))
                        self.rotated += 1
                    except OSError:
                        pass
                with path.open("a", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
                n += len(lines)
            except OSError as exc:
                # 磁盘异常不应中断采集；记录并丢弃这批（内存中仍保有最近数据）
                self.dropped += len(lines)
                self.last_error = "写入 %s 失败: %s" % (path.name, exc)
        self.written += n
        if n:
            self._prune_if_needed()
        return n

    def _prune_if_needed(self) -> None:
        """目录总量超限时删除最旧文件（保证磁盘有界）。

        遍历成本不低，所以最多每 60s 做一次。
        """
        now = time.time()
        if now - self._last_prune < 60.0:
            return
        self._last_prune = now
        assert self.root is not None
        try:
            entries = []
            total = 0
            for f in self.root.iterdir():
                if not f.is_file():
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                total += st.st_size
                entries.append((st.st_mtime, st.st_size, f))
        except OSError as exc:
            self.last_error = "扫描走势目录失败: %s" % exc
            return
        if total <= self.max_dir_bytes:
            return
        entries.sort()  # 最旧在前
        for _mt, size, f in entries:
            if total <= self.max_dir_bytes:
                break
            try:
                f.unlink()
                total -= size
                self.pruned += 1
            except OSError:
                continue

    def load(self, mid: str, limit: int = MAX_TICKS_PER_MARKET) -> List[Dict[str, Any]]:
        """读取某场的历史走势（重启后恢复用）。"""
        if not self.enabled:
            return []
        path = self._path(mid)
        if not path.exists():
            return []
        try:
            with path.open(encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError as exc:
            self.last_error = "读取 %s 失败: %s" % (path.name, exc)
            return []
        out: List[Dict[str, Any]] = []
        for line in lines[-max(1, limit):]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # 半行/脏行直接跳过
        return out

    def health(self) -> Dict[str, Any]:
        with self._lock:
            pending = sum(len(v) for v in self._buf.values())
        return {"enabled": self.enabled, "root": str(self.root) if self.root else None,
                "written": self.written, "dropped": self.dropped,
                "rotated": self.rotated, "pruned": self.pruned,
                "pending": pending, "last_error": self.last_error}


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
    #: 启动时从落盘文件恢复的盘口数（验证持久化生效）
    resumed_markets: int = 0

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
            "resumed_markets": self.resumed_markets,
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
    ts = _to_int(decoded.get("time") or decoded.get("t") or 0, 0)
    ticks: List[PriceTick] = []

    def walk_block(block: Mapping[str, Any], chpid_hint: str = "") -> None:
        chpid = str(block.get("chpid") or chpid_hint)
        hv = str(block.get("hv") or "")
        hid = str(block.get("hid") or "")
        bts = _to_int(block.get("t") or ts, ts)
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
                order_detail={
                    "matchId": mid, "marketId": hid,
                    "playId": str(block.get("hpid") or chpid),
                    "playOptions": str(entry.get("ot") or ""),
                    "playOptionsId": str(entry.get("oid") or ""),
                    "marketValue": hv, "marketTypeFinally": "EU",
                    "oddFinally": str(new_val), "matchType": 2, "sportId": "1",
                    **({"dataSource": str(entry["cds"])} if entry.get("cds") else {}),
                },
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


def _period_score_from_payload(value: Any, period: str) -> Optional[Tuple[int, int]]:
    """从推送 `msc` 的 S2/S1 片段提取阶段比分。"""
    chunks = [value] if isinstance(value, str) else value if isinstance(value, Sequence) else ()
    prefix = period + "|"
    for chunk in chunks:
        text = str(chunk).strip().strip("'")
        if not text.startswith(prefix):
            continue
        _, _, score = text.partition("|")
        left, _, right = score.partition(":")
        try:
            return int(left), int(right)
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
        max_matches: int = 0,
        trend_root: Optional[str] = None,
        resume: bool = True,
        on_price_change: Optional[Callable[[List[str]], None]] = None,
        on_state_change: Optional[Callable[[List[str]], None]] = None,
    ) -> None:
        """
        Args:
            session_provider: 提供 `Session`（含 request_id / host / origin）。
            mids_provider: 返回要订阅的赛事 ID 列表；定期刷新。
            subscribe_interval_s: 重新计算订阅列表的间隔。
            max_matches: 订阅上限；**0 表示不限制**（覆盖全部进行中）。
                不要再写固定数字：用户要求展示与乐鱼接口的进行中数量一致，
                写死 60 在赛程密集时会静默丢掉赛事。
            trend_root: 走势落盘目录；给出则启用持久化（推荐）。
                为空时仅存内存，容器重启即丢。
            resume: 启动时从落盘文件回填走势（重启不丢历史）。
            on_price_change: **盘口真实变动**回调，参数是本批变动涉及的
                赛事 ID（已去重，**按首次出现顺序**）。

                为什么需要它：原实现只按固定间隔（600s）定时决策，
                而赔率/盘口在秒级跳动 —— 定时轮询要么错过时机、要么
                在行情不动时空烧 LLM。用户要求「盘口变化时自动触发」。

                契约（调用方必须知道）：
                  * 回调在**推送消费线程**上同步调用，因此必须**极快返回**；
                    任何耗时工作（尤其是 LLM）必须自行丢进别的线程。
                  * 回调在 `_record_ticks` 的锁**之外**触发，不得再获取
                    `hub._lock`（会与外层竞争，虽为 RLock 但没必要）。
                  * 回调抛出的异常会被吞掉（见 `_notify_price_change`），
                    以免一条坏回调拖死整个推送链路。
        """
        self.session_provider = session_provider
        self.mids_provider = mids_provider
        self.subscribe_interval_s = subscribe_interval_s
        self.max_matches = max_matches
        #: 盘口变动回调（见 docstring 契约）
        self.on_price_change = on_price_change
        self.on_state_change = on_state_change

        self.stats = RealtimeStats()
        self._lock = threading.RLock()
        self._trends: Dict[Tuple[str, str, str], TrendSeries] = {}
        # 上次观测到的赔率（key = mid|chpid|hv|oid）→ 仅当变化时才记 tick
        self._last_price: Dict[Tuple[str, str, str, str], float] = {}
        self._snapshots = 0
        self._scores: Dict[str, Tuple[int, int]] = {}
        self._status: Dict[str, Dict[str, Any]] = {}
        self._events: Dict[str, Deque[Dict[str, Any]]] = {}
        self._info: Dict[str, Dict[str, Any]] = {}
        self._market_meta: Dict[str, Dict[str, Any]] = {}
        self._versions: Dict[str, int] = {}
        self._received_at: Dict[str, float] = {}
        self._status_at: Dict[str, float] = {}
        self._suspensions: Dict[str, Dict[str, float]] = {}
        self._finished: set = set()
        self._final_proofs: Dict[str, Dict[str, Any]] = {}
        # `_finished` also contains historical terminal scores restored at boot.
        # Those records must not suppress a match that appears in the current
        # schedule.  This set only tracks terminal events from this process and
        # is safe to use for the next subscription refresh.
        self._runtime_finished: set = set()
        self._mids_provider_failed = False
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._subscribed: List[str] = []
        #: 走势持久化（供经济学算法/LLM 与重启后回填使用）
        self.trend_store = TrendStore(trend_root)
        #: **内存实时赔率表**（关键：查询路径不再碰磁盘）。
        #:
        #: 与 `_last_price` 的分工：
        #:   * `_last_price` 只存“上次值/新值”用于**判定变动**（算走势）；
        #:   * `LiveBook` 存**当前值**，供看板/决策直接读取。
        #: 两者同一个写入点（`_record_ticks`），所以不会出现
        #: “走势有、实时表没有”的不一致。
        #:
        #: 落盘路径与走势同级（`_trends/../_live/live.json`），
        #: 随 output 卷一起持久化：重启后立即有本地数据可读。
        self.live = LiveBook(_live_snapshot_path(trend_root))
        self.live.load()          # 启动即回填（无需等第一批推送）
        #: **比分持久化**（结算的赛果来源）。
        #:
        #: 为何必须落盘（用户问题 2 的真正根因）：实测两条上游路径都拿不到
        #: **历史**比分 —— 赛程接口不返回 `msc`；盘口接口对已结束场次返回
        #: 空串（实测 11/11 全空）。即赛果只在“进行中”那个时间窗可得，
        #: 而结算总在结束之后 → 永远 `graded=0`。
        #: 因此在收到的当下就落盘，重启后仍可结算。
        self.scores_store = ScoreStore(_score_snapshot_path(trend_root))
        self._load_scores()
        #: mid → 最近一次收到**比分推送**（C103/C1021）的本机单调时间戳。
        #:
        #: 为什么需要：比分是判断「比赛是否已结束 / 该场推送是否还活着」的
        #: 最直接证据。若某场的比分是几分钟前收到的，而赛程抓取又失败，
        #: 单凭快照的 `state` 会把**已结束的比赛**当成进行中送去分析，
        #: 而分析层会把比分喂给 LLM —— LLM 直接“抄答案”给出虚假买入
        #: （本项目真实故障：p_llm=1.0 vs 市场 0.35，理由是“终场0:0…”）。
        self._score_at: Dict[str, float] = {}
        #: mid → 本 Hub **首次见到**该场推送的时刻（单调时钟不可回拨）。
        #:
        #: 用途：区分「从未有过比分推送、且已存在很久」的场次（几乎可以
        #: 肯定不是正在踢的）与「刚刚出现、只是比分还没推来」的场次。
        #: 单调时钟 (`time.monotonic`) 不受 NTP 回拨影响，避免误判。
        self._first_seen: Dict[str, float] = {}
        if resume:
            self._resume_from_store()

    def _load_scores(self) -> None:
        """启动时从本地回填比分与结束状态（`ScoreStore`）。

        为何需要：赛果只在“比赛进行中”那个时间窗内可取，
        重启后若只依赖上游就永远拿不回；本地文件是**唯一冗余**。
        只回填「已确认结束」的场次作为结束集合，避免把“进行中的
        当前比分”误当成终场比分（那会把还在踢的比赛算成已定输赢）。
        """
        try:
            rows = self.scores_store.load()
        except (AttributeError, TypeError):
            return
        if not rows:
            return
        with self._lock:
            for mid, rec in rows.items():
                if not isinstance(rec, Mapping):
                    continue
                ft = rec.get("ft")
                if not (isinstance(ft, (list, tuple)) and len(ft) >= 2):
                    continue
                try:
                    h, a = int(ft[0]), int(ft[1])
                except (TypeError, ValueError):
                    continue
                mid = str(mid)
                self._scores[mid] = (h, a)
                ht = rec.get("ht")
                if isinstance(ht, (list,tuple)) and len(ht)==2 and all(isinstance(v,int) and not isinstance(v,bool) and v>=0 for v in ht):
                    self._status.setdefault(mid,{})['half_score'] = list(ht)
                proof = rec.get('final_proof')
                if rec.get('done') and isinstance(proof,Mapping) and (str(proof.get('ms'))=='3' or str(proof.get('mmp'))=='999'):
                    self._finished.add(mid)
                    self._final_proofs[mid] = dict(proof)

    def _save_scores(self, force: bool = False) -> None:
        """把当前比分/结束集合落盘（节流；失败不影响推送）。

        ⚠️ **`C109`（比赛结束）必须传 `force=True`**：它会在只有几秒间隔的
        情况下紧跟 `C103`（比分）发生，若被 3s 节流丢掉，
        “已结束”这个标记就永远不落盘 —— 而它正是结算的**安全前提**
        （未标记结束的场次不得结算，否则会把还在踢的算成已定输赢）。
        本项目测试真实拓到该缺陷。
        """
        with self._lock:
            scores = dict(self._scores)
            finished = set(self._finished)
            half_scores = {mid: list(st['half_score']) for mid,st in self._status.items() if st.get('half_score')}
            final_proofs = dict(self._final_proofs)
        try:
            self.scores_store.save(scores, finished, force=force, half_scores=half_scores, final_proofs=final_proofs)
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            self.stats.last_error = "比分落盘失败: %s" % exc

    def _resume_from_store(self) -> None:
        """从落盘文件回填历史走势（重启不丢）。

        同时重建 `_last_price` 基线：否则重启后的第一条推送会被当作
        “首次观测”，丢失一次真实变动。
        """
        store = self.trend_store
        if not store.enabled or store.root is None:
            return
        try:
            files = list(store.root.glob("*.jsonl"))
        except OSError:
            return
        for path in files:
            mid = path.stem
            rows = store.load(mid, limit=MAX_TICKS_PER_MARKET)
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                try:
                    chpid = str(row.get("chpid") or "")
                    hv = str(row.get("hv") or "")
                    oid = str(row.get("oid") or "")
                    old = float(row.get("old", 0.0))
                    new = float(row.get("new", 0.0))
                    ts = int(row.get("ts") or 0)
                except (TypeError, ValueError):
                    continue
                if chpid == "" and oid == "":
                    continue
                tick = PriceTick(
                    mid=mid, chpid=chpid, hid="", hv=hv, oid=oid,
                    ot=str(row.get("ot") or ""), old_ov=old, new_ov=new,
                    ts_ms=ts)
                key = (mid, chpid, hv)
                series = self._trends.get(key)
                if series is None:
                    series = TrendSeries(mid=mid, chpid=chpid, hv=hv)
                    self._trends[key] = series
                series.add(tick)
                self._last_price[(mid, chpid, hv, oid)] = new
        if self._trends:
            self.stats.resumed_markets = len(self._trends)

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

    def _changed_state(self, mid: str) -> None:
        self._versions[mid] = self._versions.get(mid, 0) + 1
        self._received_at[mid] = time.monotonic()

    def seed_matches(self, matches: Sequence[Any]) -> None:
        """Background REST enrichment; never performed by the decision worker."""
        mids = []
        now = time.monotonic()
        with self._lock:
            for match in matches:
                mid = str(match.mid)
                self._info[mid] = {
                    "match_id": mid, "league": match.tournament,
                    "home": match.home, "away": match.away,
                    "sport": match.sport, "sport_id": match.sport_id,
                }
                ticks = []
                meta = self._market_meta.setdefault(mid, {})
                for market in getattr(match, "markets", ()):
                    meta[str(market.chpid)] = {"name": market.name, "hpt": market.hpt}
                    for q in market.quotes:
                        ot = {"home": "1", "away": "2", "draw": "X",
                              "over": "Over", "under": "Under"}.get(q.outcome, q.label or q.oid)
                        ticks.append(PriceTick(mid, str(market.chpid), market.market_id, market.hv,
                                               q.oid, ot, q.decimal, q.decimal,
                                               q.ctsp or market.ctsp,
                                               order_detail={
                                                   "matchId": mid, "marketId": market.market_id,
                                                   "playId": market.play_id or str(market.chpid),
                                                   "playOptions": ot, "playOptionsId": q.oid,
                                                   "marketValue": market.hv, "marketTypeFinally": "EU",
                                                   "oddFinally": str(q.decimal), "matchType": 2 if match.is_live else 1,
                                                   "sportId": str(match.sport_id), "dataSource": q.source,
                                                   "tournamentId": str(match.tid),
                                               }))
                self.live.upsert_many(ticks)
                if match.is_finished or now - self._status_at.get(mid, 0) > 10:
                    self._status[mid] = {**self._status.get(mid, {}),
                                         "ms": getattr(match, "status", 1 if not match.is_finished else 3),
                                         "mst": match.minute, "mmp": match.period}
                    half = getattr(match, "half_score", (None, None))
                    if half and half[0] is not None and half[1] is not None:
                        self._status[mid]["half_score"] = [int(half[0]), int(half[1])]
                    self._status_at[mid] = now
                if match.is_finished or now - self._score_at.get(mid, 0) > 10:
                    from core.live_model import valid_score
                    score = valid_score(match.score)
                    if score is not None:
                        self._scores[mid] = score
                        self._score_at[mid] = now
                if match.is_finished:
                    self._final_proofs[mid] = {'ms':match.status,'mmp':match.period}
                    self._finished.add(mid)
                    self._runtime_finished.add(mid)
                else:
                    # 当前赛程是权威状态：旧快照/旧进程留下的终场记录不能
                    # 把这次仍在进行的赛事标成 finished，或阻断实时分析。
                    self._finished.discard(mid)
                    self._final_proofs.pop(mid,None)
                    self._runtime_finished.discard(mid)
                self._changed_state(mid)
                mids.append(mid)
        self._notify_state_change(mids)
        self._save_scores(force=True)

    def decision_snapshot(self, mid: str) -> Dict[str, Any]:
        """Copy score, status and quotes under the same writer lock."""
        with self._lock:
            return {
                "match_id": mid, "info": dict(self._info.get(mid, {})),
                "version": self._versions.get(mid, 0),
                "received_at": self._received_at.get(mid, 0),
                "score": self._scores.get(mid),
                "half_score": self.half_score(mid),
                "score_age_s": self.score_age_s(mid),
                "status_age_s": (time.monotonic() - self._status_at[mid]
                                 if mid in self._status_at else None),
                "status": dict(self._status.get(mid, {})),
                "finished": mid in self._finished,
                "suspended": bool(self._suspensions.get(mid)),
                "suspended_ids": list(self._suspensions.get(mid, {})),
                "events": [dict(e) for e in self._events.get(mid, ())],
                "quotes": self.live.book(mid),
                "market_meta": dict(self._market_meta.get(mid, {})),
            }

    def state_version(self, mid: str) -> int:
        with self._lock:
            return self._versions.get(mid, 0)

    def _notify_state_change(self, mids: Sequence[str]) -> None:
        if self.on_state_change is None or not mids:
            return
        try:
            self.on_state_change(list(dict.fromkeys(mids)))
        except Exception as exc:  # noqa: BLE001
            self.stats.last_error = "on_state_change: %s: %s" % (type(exc).__name__, exc)

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

    def score_age_s(self, mid: str) -> Optional[float]:
        """最近一次收到该场比分推送距今秒数；从未收到过返回 None。

        用途：分析层据此判断「比分是否新鲜」。赛程接口失败时它是唯一
        能区分「正在踢」与「早就结束了但快照还标 active」的信号，
        从而避免把终场比分泄漏给 LLM（见 `_build_context`）。
        """
        with self._lock:
            at = self._score_at.get(str(mid))
        if not at:
            return None
        return max(0.0, time.monotonic() - at)

    def first_seen_age_s(self, mid: str) -> Optional[float]:
        """本 Hub 首次见到该场推送距今秒数；从未见过返回 None。"""
        with self._lock:
            at = self._first_seen.get(str(mid))
        if not at:
            return None
        return max(0.0, time.monotonic() - at)

    def _touch_seen(self, mid: str) -> None:
        """记录“首次见到该场”（调用方持锁）。"""
        if mid and mid not in self._first_seen:
            self._first_seen[mid] = time.monotonic()

    def status(self, mid: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._status.get(mid) or {})

    def events(self, mid: str) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._events.get(mid) or ())

    def is_finished(self, mid: str) -> bool:
        with self._lock:
            return mid in self._finished

    def finished_mids(self) -> List[str]:
        """已收到结束通知（`C109`）的赛事 ID。

        用途：结算时作为**本地方案的来源**（见 `settle_finished`）。
        用户要求「正确率要能统计」，而 REST 赛程在会话过期时拿不到；
        推送里的 `C109` 同样能告诉我们哪些场已结束。
        """
        with self._lock:
            return sorted(self._finished)

    def scores_snapshot(self) -> Dict[str, Tuple[int, int]]:
        """当前所有已知比分（本地推送累积，**不依赖 REST**）。

        为何需要（用户报「买入决策正确率没有统计」的根因之一）：
        结算原来只调 `source.schedule()` 拿终场比分；会话一过期就
        完全拿不到 → `graded=0` → 命中率永远算不出。
        而推送里的比分（`C103`/`C1021`）本就存在内存里，
        够用于结算已结束的场次。
        """
        with self._lock:
            return dict(self._scores)

    def half_score(self, mid: str) -> Optional[Tuple[int, int]]:
        """半场比分（若上游在状态里给过）；缺失返回 None。

        半场比分缺失时结算会把半场盘口判为 `void`（而不是拿全场
        比分硬算）—— 这是既有 `core.settlement` 的约定。
        """
        with self._lock:
            st = self._status.get(str(mid)) or {}
        raw = st.get("half_score")
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            try:
                return (int(raw[0]), int(raw[1]))
            except (TypeError, ValueError):
                return None
        return None

    def subscribed(self) -> List[str]:
        with self._lock:
            return list(self._subscribed)

    def health(self) -> Dict[str, Any]:
        with self._lock:
            baseline = len(self._last_price)
            snaps = self._snapshots
        try:
            session = self.session_provider.describe()
        except Exception as exc:  # noqa: BLE001 - health must remain available
            session = {"provider": type(self.session_provider).__name__,
                       "describe_error": type(exc).__name__}
        return {
            "running": self.running,
            "session": session,
            # 已建立基线的赔率选项数：对比它可判断推送是否在正常覆盖盘口
            "price_options": baseline,
            # 收到的赔率快照条数（含未变化的），用于区分“推送正常但没变化”
            "price_snapshots": snaps,
            "changed_ratio": round(self.stats.price_ticks / snaps, 4) if snaps else 0.0,
            "trend_store": self.trend_store.health(),
            # 内存实时表：用户要求「采集落本地、查询读本地」的落地情况。
            # 看 `rows`/`matches` 能直接回答“现在到底有多少场在看”
            # （与 leyu 页面对账时最有用），`oldest_age_s` 则能看出
            # 行情是否已经停摆（本次故障中它一度是 19.9 小时）。
            "live_book": self.live.health(),
            **self.stats.as_dict(),
        }

    # -- 内部 ---------------------------------------------------------------

    def _record_ticks(self, ticks: Sequence[PriceTick]) -> None:
        """记录赔率变动。

        关键：`C105` 是周期全量快照（`obv` 恒等于 `ov`），所以必须用
        **自行维护的上次值** 对比，只把真正变化的项记为走势。
        否则每条快照都入库，真实信号会被稀释上千倍。

        真实变动会同步落盘（`trend_store`），供后续分析与重启回填。

        ⚠️ **同时把本批的全部当前值写入内存实时表**（`self.live`）：
        `C105` 既然是周期性全量快照，那么每条推送里的 `ov` 就是
        **当前赔率**。早期实现只把变化项用于算走势，当前值算完即丢，
        查询时被迫回磁盘翻不可变历史（既慢又易取到过期价）。

        注意：传给实时表的是 **`ticks`（全量）**，不是 `changed`（仅变动）：
        前者才是“现在每个选项多少钱”，后者只是“哪些刚跳过”。
        """
        # 实时表用全量 ticks（当前值），必须在过滤“变动”之前就写。
        changed: List[PriceTick] = []
        with self._lock:
            self.live.upsert_many(ticks)
            for mid in dict.fromkeys(t.mid for t in ticks):
                self._changed_state(mid)
            for t in ticks:
                suspended = self._suspensions.get(t.mid, {})
                for pid, at in list(suspended.items()):
                    if pid in (t.hid, t.chpid) and t.ts_ms > at * 1000:
                        suspended.pop(pid, None)
            for t in ticks:
                self._touch_seen(t.mid)
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
                changed.append(real)
            self._snapshots += len(ticks)
            self.stats.price_ticks += len(changed)
        # 落盘在锁外：磁盘 IO 不应阻塞推送消费
        self._notify_state_change([t.mid for t in ticks])
        if changed:
            self._notify_price_change(changed)
            self.trend_store.append_many(changed)
        # 实时表落盘（节流 5s；内部自会判断是否需要写）。
        # 放在锁外，与走势落盘同理：磁盘 IO 不应阻塞推送消费。
        if ticks:
            self.live.save()

    def _notify_price_change(self, changed: Sequence[PriceTick]) -> None:
        """把「哪些赛事的盘口变了」告诉上层（触发决策用）。

        在锁外调用；回调异常只记录不抛出 —— 决策调度是**增强**能力，
        绝不能因为它的 bug 把整条实时推送链路打死（那样连行情都看不到了）。
        """
        cb = self.on_price_change
        if cb is None or not changed:
            return
        mids: List[str] = []
        seen: set = set()
        for t in changed:
            if t.mid not in seen:
                seen.add(t.mid)
                mids.append(t.mid)
        try:
            cb(mids)
        except Exception as exc:  # noqa: BLE001 - 回调失败不得影响推送
            self.stats.last_error = "on_price_change: %s: %s" % (
                type(exc).__name__, exc)

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
            dq.append({**dict(event), "received_at": time.time()})

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

        if cmd in ("C101", "C103", "C1021"):
            decoded = decode_push_payload(msg.get("cd"))
            if isinstance(decoded, Mapping):
                parsed = parse_c103(decoded)
                if parsed:
                    mid, score = parsed
                    half = _period_score_from_payload(decoded.get("msc"), "S2")
                    with self._lock:
                        self._touch_seen(mid)
                        self._scores[mid] = score
                        self._score_at[mid] = time.monotonic()
                        if half is not None:
                            self._status.setdefault(mid, {})["half_score"] = list(half)
                        self._changed_state(mid)
                    self.stats.score_updates += 1
                    # 落盘赛果（见 ScoreStore：结束之后就再也拿不到了）
                    self._notify_state_change([mid])
                    self._save_scores()
                self._record_event(str(decoded.get("mid", "")),
                                   {"cmd": cmd, "cmec": decoded.get("cmec"),
                                    "mmp": decoded.get("mmp"), "mst": decoded.get("mst")})
            return

        if cmd == "C102":
            decoded = decode_push_payload(msg.get("cd"))
            if isinstance(decoded, Mapping):
                mid = str(decoded.get("mid", ""))
                with self._lock:
                    self._touch_seen(mid)
                    self._status[mid] = {
                        **self._status.get(mid, {}),
                        "cmec": decoded.get("cmec"),
                        "mmp": decoded.get("mmp"),
                        "mst": decoded.get("mst"),
                        "ha": decoded.get("ha"),
                    }
                    half = _period_score_from_payload(decoded.get("msc"), "S2")
                    if half is not None:
                        self._status[mid]["half_score"] = list(half)
                    final = str(decoded.get('mmp'))=='999' or str(decoded.get('ms'))=='3'
                    if final:
                        self._final_proofs[mid] = {'ms':decoded.get('ms'),'mmp':decoded.get('mmp')}
                        final_score = _period_score_from_payload(decoded.get('msc'), 'S1')
                        if final_score is not None:
                            self._scores[mid] = final_score
                        self._finished.add(mid)
                        self._runtime_finished.add(mid)
                    self._status_at[mid] = time.monotonic()
                    self._changed_state(mid)
                self._save_scores(force=final)
                self._notify_state_change([mid])
                self.stats.status_updates += 1
                self._record_event(mid, {"cmd": cmd, "cmec": decoded.get("cmec")})
            return

        if cmd == "C109":
            decoded = decode_push_payload(msg.get("cd"))
            items = decoded if isinstance(decoded, Sequence) else ()
            with self._lock:
                for it in items:
                    if isinstance(it, Mapping):
                        mid = str(it.get("mid", ""))
                        # C109 is removal, which can also mean cancelled/closed.
                        # Only an explicit final status certifies the score.
                        if str(it.get('ms'))=='3' or str(it.get('mmp'))=='999':
                            self._finished.add(mid)
                            self._final_proofs[mid] = {'ms':it.get('ms'),'mmp':it.get('mmp')}
                            final_score = _period_score_from_payload(it.get('msc'),'S1')
                            if final_score is not None:
                                self._scores[mid]=final_score
                        self._runtime_finished.add(mid)
                        self._changed_state(mid)
            self._notify_state_change([str(it.get("mid", ""))
                                       for it in items if isinstance(it, Mapping)])
            self.stats.finished += len(items)
            # **关键**：比赛刚结束时把终场比分与结束标记落盘。
            # 这是结算**唯一**能拿到过时赛果的时机（之后上游不再提供），
            # 且必须 `force=True` —— 不能因节流丢掉“已结束”标记（见 _save_scores）。
            self._save_scores(force=True)
            return

        if cmd == "C110":
            decoded = decode_push_payload(msg.get("cd"))
            if isinstance(decoded, Mapping):
                mid = str(decoded.get("mid", ""))
                self._record_event(mid, {"cmd": cmd, "mc": decoded.get("mc")})
                with self._lock:
                    self._changed_state(mid)
                self._notify_state_change([mid])
            return

        if cmd == "C104":
            decoded = decode_push_payload(msg.get("cd"))
            if isinstance(decoded, Mapping):
                mid = str(decoded.get("mid", ""))
                self._record_event(mid, {"cmd": cmd, "hpid": decoded.get("hpid"),
                                         "hs": decoded.get("hs"), "ms": decoded.get("ms")})
                with self._lock:
                    self._changed_state(mid)
                self._notify_state_change([mid])
            return  # Unknown schema is not evidence that the entire match is paused.

        if cmd == "C303":
            decoded = decode_push_payload(msg.get("cd"))
            if isinstance(decoded, Mapping):
                mid = str(decoded.get("mid", ""))
                with self._lock:
                    pid = str(decoded.get("hpid") or decoded.get("chpid") or "unknown")
                    self._suspensions.setdefault(mid, {})[pid] = time.time()
                    self._status.setdefault(mid, {})["suspended_hpid"] = pid
                    self._changed_state(mid)
                self._notify_state_change([mid])
                self._record_event(mid, {"cmd": cmd, "hpid": decoded.get("hpid")})
            return

    def _pick_mids(self) -> List[str]:
        if self.mids_provider is None:
            return []
        self._mids_provider_failed = False
        try:
            mids = [str(m) for m in (self.mids_provider() or ())]
        except Exception as exc:  # noqa: BLE001 - 订阅来源失败不应终止推送线程
            self.stats.last_error = "mids_provider 失败: %s" % exc
            self._mids_provider_failed = True
            return []
        # 当前赛程是权威状态。不能把启动时从历史比分文件恢复的
        # `_finished` 集合用于过滤，否则今天重新出现的 mid 会被永久漏掉。
        # 只过滤本进程刚收到的终场事件，避免在上游状态刷新前继续订阅已结束场。
        with self._lock:
            mids = [m for m in dict.fromkeys(mids)
                    if m not in self._runtime_finished]
        # max_matches <= 0 表示不截断（全部订阅）
        cap = _to_int(self.max_matches)
        return mids[:cap] if cap > 0 else mids

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

            feed = LEYUFeed(ws_url, session.request_id,
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
                        provider_failed = self._mids_provider_failed
                        should_clear = not new_mids and not provider_failed
                        if ((new_mids and new_mids != self._subscribed)
                                or (should_clear and self._subscribed)):
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
