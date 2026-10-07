#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
可插拔数据源（AGENTS.md §3.2：采集层必须**数据源无关**）。

设计要点
--------
本项目历史上把「体彩官方 API 落盘 JSON」硬编码进了 `ValuationService`
（`load_corpus` 只认 `场次*.json`）。要换成乐鱼源，就必须先把这一步抽象出来，
否则每换一次数据源都要改下游服务。

    SnapshotSource（协议）
      └── LEYUSource        乐鱼 API（在线 HTTP / saz 离线回放）

> 2026-10 起本项目**只保留乐鱼源**：体彩文件源（`TicaiFileSource`）与其
> payload 解析器（`collector/normalizer.py`）已随「只保留 leyu 相关」清理删除。

`LEYUSource` 内部复用 `LEYUClient`（HTTP）与 `leyu_ws`（实时推送），
**不在本层发起任何浏览器操作**。

乐鱼源是**两阶段**的（协议限制，见 docs/architecture/leyu-api-protocol.md）：

    阶段 1  getOriginalDataPB            → 全部赛程（不含赔率，一次 1868 场）
    阶段 2  structureMatchBaseInfoByMidsPB → 按 mid 批量取完整盘口（含全部盘口线）

因此 `fetch()` 支持 `mids` 限定与 `max_matches` 上限，避免默认情况下
把上千场全部拉一遍。
"""

from __future__ import annotations

import json
import os
import time
import zipfile
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from core.models import OddsSnapshot, utcnow

from .leyu_client import (
    DEFAULT_HOST,
    DEFAULT_ORIGIN,
    AuthError,
    LEYUClient,
    LEYUMatch,
    decode_envelope,
    parse_match_list,
    parse_odds_block,
)
from .session import (
    Session,
    SessionProvider,
    make_session_provider,
)
from .leyu_normalizer import DEFAULT_SOURCE as LEYU_DISPLAY_SOURCE
from .leyu_normalizer import (
    DEFAULT_STALE_AFTER,
    snapshots_from_match,
)

__all__ = [
    "SOURCE_LEYU",
    "SOCCER_SPORT_ID",
    "KNOWN_SOURCES",
    "SourceError",
    "SnapshotSource",
    "LEYUSource",
    "resolve_source_name",
    "make_source",
]

#: 数据源标识（本项目仅乐鱼）
SOURCE_LEYU = "leyu"

#: 足球运动 ID（乐鱼 `spList` 中 csid="1"）。
#: 本项目是足球估值系统；同一网关还返回篮球/网球/乒乓球等，必须默认过滤。
SOCCER_SPORT_ID = "1"

#: 非全量拉取时的默认上限（安全阀：避免误拉两千多场）。
#: 需要全部赛事时必须显式 `full=True`。
#: 注意：**进行中赛事的订阅/决策不走这个上限** —— 它们应当覆盖全部
#: （见 `SnapshotSource.live_match_ids` 与 `RealtimeHub`）。
DEFAULT_PARTIAL_MAX = 200

#: 「进行中」的开赛前宽限（秒）。
#:
#: 乐鱼页面「滚球」计数比单纯 `ms==1` 多几场，实测差额恰好等于
#: 「已临近开赛但上游尚未置 `ms=1`」的场次（详见 `live_match_ids` 文档）。
#: 900s = 15 分钟：实测可完全消除该系统性偏差，再放大则开始超额。
#: 设为 0 可退回到「只认 `ms==1`」的严格口径。
LIVE_GRACE_S = 900.0


def _now_ms() -> int:
    """当前毫秒时间戳（与乐鱼 `mgt` 同一口径）。"""
    return time.time_ns() // 1_000_000


def _counts_as_live(m: Any, now_ms: int, grace_s: float) -> bool:
    if getattr(m, "is_live", False):
        return True
    if grace_s <= 0 or getattr(m, "is_finished", False):
        return False
    start = _to_int(getattr(m, "start_ms", 0))
    return bool(start) and start <= now_ms + grace_s * 1000

KNOWN_SOURCES: Tuple[str, ...] = (SOURCE_LEYU,)

#: 别名 → 规范名（兼容中文标识与历史写法）
_SOURCE_ALIASES: Mapping[str, str] = {
    "leyu": SOURCE_LEYU,
    "乐鱼": SOURCE_LEYU,
    "乐鱼api": SOURCE_LEYU,
    "乐鱼官方api": SOURCE_LEYU,
}


class SourceError(Exception):
    """数据源读取/解析失败。"""


def _to_int(value: object, default: int = 0) -> int:
    """宽松整数转换（配置/入参可能来自环境变量字符串）。"""
    try:
        return int(str(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _to_opt_int(value: object) -> Optional[int]:
    """宽松整数转换；缺失或非法返回 None（与 `_to_int` 的区别是不给默认值）。

    用于解析上游 JSON 里的可选计数字段：缺失必须能区分于 0。
    """
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError, OverflowError):
        return None


def resolve_source_name(name: Optional[str]) -> str:
    """把用户输入的源名归一为规范名（不区分大小写与中英文别名）。"""
    if name is None or not str(name).strip():
        return SOURCE_LEYU
    key = str(name).strip().lower()
    if key in _SOURCE_ALIASES:
        return _SOURCE_ALIASES[key]
    raise SourceError(
        "未知数据源 %r；可用：%s（别名：%s）"
        % (name, ", ".join(KNOWN_SOURCES), ", ".join(sorted(_SOURCE_ALIASES)))
    )


# --------------------------------------------------------------------------- #
# 协议
# --------------------------------------------------------------------------- #

class SnapshotSource(ABC):
    """数据源协议：产出 `(快照列表, 告警列表)`。

    子类必须实现 `fetch()`。两个名称含义不同，不可混用：

    * `name`           —— **规范源 ID**（`leyu` / `ticai`），用于配置与路由；
    * `display_source` —— 写入 `OddsSnapshot.source` 的**展示名**
      （`乐鱼API` / `体彩官方API`），用于存储分组与审计展示。
    """

    #: 规范源 ID（leyu / ticai）
    name: str = "unknown"

    #: 写入快照的展示源名
    display_source: str = "unknown"

    @abstractmethod
    def fetch(
        self,
        mids: Optional[Sequence[str]] = None,
        max_matches: Optional[int] = None,
        stale_after: float = DEFAULT_STALE_AFTER,
        captured: Optional[datetime] = None,
    ) -> Tuple[List[OddsSnapshot], List[str]]:
        """拉取并归一化为快照。

        Returns:
            `(snapshots, issues)`；issues 为可读告警（不中断整批）。
        """

    def schedule(self) -> List[Any]:
        """列出当前赛程（**可选能力**）。

        返回 `LEYUMatch` 列表；不支持赛程列举的数据源（如本地文件源）
        返回空列表。后台实时推送依赖它来选取订阅目标，因此必须在基类
        声明，而不是调用处用 `getattr` 绕过类型检查。
        """
        return []

    def live_match_ids(self, sport_id: str = SOCCER_SPORT_ID,
                       grace_s: float = LIVE_GRACE_S) -> List[str]:
        """列出全部**进行中**赛事的 ID（可选能力，默认从 `schedule()` 推导）。

        语义：该列表必须覆盖乐鱼页面「滚球」列表里的全部赛事，
        不能被任何采集上限截断（实时订阅与定时决策都依赖它）。

        ## 为何需要 `grace_s`（实测对账结论，非拍脑袋）

        乐鱼页面「滚球」的计数（`platformsSportCountPB` →
        `TY.1.balls.<sport>.ct`）比单纯的 `ms==1` **多几场**：
        实测这差额恰好等于「**已临近开赛但上游尚未置 `ms=1`**」的场次
        （某次实测：`ms==1` 足球 44 场，+3 场开赛在 0~10 分钟内的 → 47，
        与上游计数完全吻合；窗口放宽到 20 分钟以上则超额）。

        因此把这类「即将开赛」的赛事一并算作进行中，才能与页面展示一致。
        上游计数本身是秒级变动的，追求整数相等不现实，
        但**把系统性偏差消除**是能做到的（见 `live_count` 的 `source` 字段）。

        Args:
            sport_id: 运动筛选；默认足球。传 `""` 关闭筛选（全部运动）。
            grace_s: 开赛前宽限秒数（默认 900s）；0 表示只认 `ms==1`。
        """
        want = SOCCER_SPORT_ID if sport_id is None else str(sport_id)
        now_ms = _now_ms()
        out: List[str] = []
        for m in self.schedule():
            if want and str(getattr(m, "sport_id", "")) != want:
                continue
            if not _counts_as_live(m, now_ms, grace_s):
                continue
            mid = str(getattr(m, "mid", ""))
            if mid:
                out.append(mid)
        return out

    def live_count(self, sport_id: str = SOCCER_SPORT_ID,
                   grace_s: float = LIVE_GRACE_S) -> Dict[str, Any]:
        """进行中赛事计数（供 `/health` 与前端对账「是否与乐鱼一致」）。

        `source` 字段说明该计数的口径：
          * `upstream` —— 上游自报计数（`platformsSportCountPB`）可用，
            此时 `live` 即上游值，与本系统推导值并列便于对账；
          * `derived`  —— 上游计数不可用，`live` 由本系统赛程推导。

        Returns:
            `{"live": N, "live_all_sports": M, "scheduled": K,
              "source": "upstream"|"derived", "upstream": {...}|None}`
        """
        want = SOCCER_SPORT_ID if sport_id is None else str(sport_id)
        schedule = self.schedule()
        now_ms = _now_ms()
        live_all = [m for m in schedule if _counts_as_live(m, now_ms, grace_s)]
        derived_sport = len([m for m in live_all
                             if not want
                             or str(getattr(m, "sport_id", "")) == want])

        upstream = self._upstream_live_count(want)
        return {
            "live": (upstream or {}).get("sport", derived_sport),
            "derived": derived_sport,
            "live_all_sports": len(live_all),
            "scheduled": len([m for m in schedule
                              if not want
                              or str(getattr(m, "sport_id", "")) == want]),
            "source": "upstream" if upstream else "derived",
            "upstream": upstream,
        }

    def _upstream_live_count(self, sport_id: str) -> Optional[Dict[str, Any]]:
        """取上游自报的「滚球」计数（可选能力；不支持时返回 None）。

        端点：`GET /yewu11/v1/m/platformsSportCountPB?merchantCode=Y&enName=YBTY`
        结构：`{"0": {"TY": {"1": {"ct": N, "balls": {"1": {"ct": n, …}}}}}}`
        其中 `TY`=体育、`"1"`=滚球、`balls` 按运动 ID 细分。
        """
        return None

    def describe(self) -> Dict[str, Any]:
        """数据源元信息（供 /health 与排障使用）。"""
        return {"source": self.name, "kind": type(self).__name__}


# --------------------------------------------------------------------------- #
# 乐鱼源
# --------------------------------------------------------------------------- #

def _saz_response_body(path: str, sid: int) -> str:
    """从 saz 归档取某会话的响应体（兼容 CRLF/LF）。"""
    with zipfile.ZipFile(path) as z:
        text = z.read("raw/%03d_s.txt" % sid).decode("utf-8", "replace")
    idx = text.find("\r\n\r\n")
    if idx < 0:
        idx = text.find("\n\n")
        return text[idx + 2:] if idx >= 0 else ""
    return text[idx + 4:]


class LEYUSource(SnapshotSource):
    """乐鱼源：在线（HTTP）或离线（saz 回放）产出快照。

    Args:
        host: 网关地址；在线模式下可用 `decode_prod_json()` 解密 prod.json 得到。
        origin: 前端来源（写入 Origin/Referer，服务端按此做 CORS）。
        request_id: 32 位 hex 会话标识；缺省随机。
        replay: saz 路径；给定时走离线回放，**不发起任何网络请求**。
        replay_sids: 回放用的 (赛程会话, 盘口会话) 号；默认取抓包实证值。
        batch_size: 在线批量拉盘口的批大小（抓包为 12~20）。
    """

    name = SOURCE_LEYU
    display_source = LEYU_DISPLAY_SOURCE

    #: 抓包实证会话号：347=GET getOriginalDataPB，426=POST structureMatchBaseInfoByMidsPB
    DEFAULT_REPLAY_SIDS: Tuple[int, int] = (347, 426)

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        origin: str = DEFAULT_ORIGIN,
        request_id: Optional[str] = None,
        replay: Optional[str] = None,
        replay_sids: Optional[Tuple[int, int]] = None,
        batch_size: int = 20,
        timeout: float = 15.0,
        session_provider: Optional[SessionProvider] = None,
        cuid: Optional[str] = None,
    ) -> None:
        self.host = host
        self.origin = origin
        self.request_id = request_id
        self.cuid_override = cuid
        self.replay = replay
        self.replay_sids = replay_sids or self.DEFAULT_REPLAY_SIDS
        self.batch_size = max(1, _to_int(batch_size, 20))
        self.timeout = timeout
        self._client: Optional[LEYUClient] = None
        # 进度回调出错时的记录（供排障，不影响采集）
        self.last_progress_error = ""
        # 会话管理：显式传入优先；否则按「命令 → 文件 → 环境变量」构造
        self.session_provider = session_provider if session_provider is not None \
            else make_session_provider(request_id=request_id)
        self._session: Optional[Session] = None
        self.session_refreshes = 0

    # -- 会话 -------------------------------------------------------------

    @property
    def session(self) -> Session:
        """当前会话；尚未取得或已超期时向 provider 取一次。"""
        if self._session is None or self._session.expired:
            self._session = self.session_provider.acquire(self._session)
            self.session_refreshes += 1
            # 会话可携带自己的网关与 Origin（不同会话可能绑定不同网关）
            if self._session.host:
                self.host = self._session.host
                self._client = None
            if self._session.origin:
                self.origin = self._session.origin
                self._client = None
        return self._session

    def _refresh_session(self) -> None:
        """标记当前会话失效并立即重取（登录命令会被重新执行）。"""
        self.session_provider.invalidate(self._session)
        self._session = None
        self._client = None
        _ = self.session  # 触发重新 acquire；失败则抛 SessionError

    # -- 内部 ---------------------------------------------------------------

    @property
    def client(self) -> LEYUClient:
        if self._client is None:
            sess = self.session
            self._client = LEYUClient(
                host=self.host, origin=self.origin,
                request_id=sess.request_id,
                cuid=self.cuid_override or sess.cuid or None,
                timeout=self.timeout,
                # 会话里携带的 Cookie 必须透传给客户端。
                # 早期版本 Session 存了 cookie 却没传出，
                # 导致注入的 cookie 完全不起作用（存了不发等于没存）。
                cookie=sess.cookie or "",
            )
        return self._client

    def _call_with_refresh(self, fn: Callable[[], Any], attempts: int = 2) -> Any:
        """发起请求；遇会话失效则续期后重试。

        乐鱼会话失效的业务码是 `0401013`（`AuthError`）。
        这里只对**会话失效**做续期重试；其它业务错误（如赛事已结束）直接上抛，
        避免把业务问题误当成鉴权问题而反复重新登录。
        """
        last: Optional[Exception] = None
        for i in range(max(1, attempts)):
            try:
                return fn()
            except AuthError as exc:
                last = exc
                if i + 1 >= attempts:
                    break
                self._refresh_session()
        assert last is not None
        raise last

    def _replay_blob(self, sid: int) -> Any:
        if not self.replay or not os.path.exists(self.replay):
            raise SourceError("抓包文件不可用: %r" % (self.replay,))
        body = _saz_response_body(self.replay, sid)
        if not body:
            raise SourceError("会话 %03d 无响应体" % sid)
        try:
            return decode_envelope(json.loads(body))
        except (ValueError, KeyError) as exc:
            raise SourceError("会话 %03d 解析失败: %s" % (sid, exc)) from exc

    def schedule(self, replay: bool = False) -> List[LEYUMatch]:
        """阶段 1：全量赛程（不含赔率）。"""
        if replay or self.replay:
            return parse_match_list(self._replay_blob(self.replay_sids[0]))
        return self._call_with_refresh(lambda: self.client.all_matches())

    def odds(
        self, mids: Sequence[str], replay: bool = False,
        progress: Optional[Any] = None,
    ) -> List[LEYUMatch]:
        """阶段 2：按 mid 批量取完整盘口。

        注意：回放模式下 `mids` 被忽略（抓包里就是一个已拉好的批次）。

        Args:
            progress: 可选回调 `fn(done, total)`，每批完成后上报进度。
                全量采集有百来批，无进度反馈时无法判断是否卡住。
        """
        if replay or self.replay:
            return parse_odds_block(self._replay_blob(self.replay_sids[1]))
        if not mids:
            return []
        out: List[LEYUMatch] = []
        total = len(mids)
        batches = list(self.client.iter_odds_batches(list(mids), self.batch_size))
        done = 0
        for batch in batches:
            out.extend(batch)
            done += len(batch)
            if progress is not None:
                try:
                    progress(done, total)
                except Exception as exc:  # noqa: BLE001 - 进度回调不应影响采集
                    # 静默吞掉会让进度上报的 bug 永不被发现；记录但不中断采集
                    self.last_progress_error = "%s: %s" % (type(exc).__name__, exc)
        return out
    # -- 主入口 -------------------------------------------------------------

    def fetch(
        self,
        mids: Optional[Sequence[str]] = None,
        max_matches: Optional[int] = None,
        stale_after: float = DEFAULT_STALE_AFTER,
        captured: Optional[datetime] = None,
        sport_id: Optional[str] = None,
        full: bool = False,
        progress: Optional[Any] = None,
    ) -> Tuple[List[OddsSnapshot], List[str]]:
        """拉取并归一化乐鱼数据。

        参数语义：
            mids: 指定赛事；为空时按赛程自动选。
            max_matches: 上限。**未指定且 full=False 时默认 60**（避免误拉两千场）；
                `full=True` 表示全量（不截断），用于完整采集。
            stale_after: 陈旧阈值（秒）。
            captured: 采集时刻（UTC），默认当前时间。
            sport_id: 限定运动（默认 `SOCCER_SPORT_ID`="1" 足球）。
                本项目是足球估值系统，而乐鱼一个网关同时返回篮球/网球/乒乓球等，
                它们的盘口代号（153/172/202/24x…）与足球完全不同；
                不筛运动会导致大量「无法映射盘口」噪音并把无关赛事写进库里。
                传 `""`（空串）可关闭筛选，拉全部运动。
            full: 是否全量采集（不限制场次）。实测 2233 场约需 111 批 / ~40 秒。
            progress: 可选回调 `fn(done, total)`，用于上报采集进度。
        """
        offline = bool(self.replay)
        when = captured or utcnow()
        want_sport = SOCCER_SPORT_ID if sport_id is None else str(sport_id)

        issues: List[str] = []
        if offline:
            matches = self.odds([], replay=True)
        else:
            schedule = self.schedule()
            # full=True 时不做截断；显式给了 max_matches 则仍以其为准。
            # 默认（full=False）也只对**非全量拉取**设一个安全上限，
            # 避免误把两千多场全拉一遍。需要全部时传 full=True。
            effective_max = max_matches if max_matches is not None else (
                None if full else DEFAULT_PARTIAL_MAX)
            targets = self._select_mids(schedule, mids, effective_max, want_sport)
            matches = self.odds(targets, progress=progress)
            missing = set(targets) - {m.mid for m in matches}
            if missing:
                issues.append("上游未返回盘口：%s" % ",".join(sorted(missing)[:20]))

        # 离线回放也要按运动过滤（除非显式要全部）
        if want_sport:
            matches = [m for m in matches if m.sport_id == want_sport]

        snaps: List[OddsSnapshot] = []
        for m in matches:
            snaps.extend(snapshots_from_match(m, when, stale_after,
                                              source=self.display_source,
                                              issues=issues))
        return snaps, issues

    @staticmethod
    def _select_mids(
        schedule: Sequence[LEYUMatch],
        mids: Optional[Sequence[str]],
        max_matches: Optional[int],
        sport_id: str = SOCCER_SPORT_ID,
    ) -> List[str]:
        """选待拉盘口的赛事。

        显式 `mids` 优先；否则取**进行中优先、开赛时间升序**的未结束赛事，
        并按运动过滤（默认只取足球，避免拉入篮球/网球的无关盘口）。

        `max_matches=None` 表示不截断（全量）；调用方需自行决定是否全量，
        因为全量会打上百批请求。
        """
        if mids:
            return [str(m) for m in mids]
        pool = [m for m in schedule if not m.is_finished]
        if sport_id:
            pool = [m for m in pool if m.sport_id == sport_id]
        # 进行中优先（实时价值最高），再按开赛时间
        pool.sort(key=lambda m: (0 if m.is_live else 1, m.start_ms, m.mid))
        if max_matches is None:
            return [m.mid for m in pool]
        return [m.mid for m in pool[: max(0, _to_int(max_matches))]]

    def _upstream_live_count(self, sport_id: str) -> Optional[Dict[str, Any]]:
        """取上游自报的「滚球」计数（乐鱼页面计数徽标同源）。

        端点：`GET /yewu11/v1/m/platformsSportCountPB?merchantCode=Y&enName=YBTY`
        结构：`{"0": {"TY": {"1": {"ct": N, "balls": {"1": {"ct": n, …}}}}}}`
        其中 `0`=今日、`TY`=体育、`"1"`=滚球、`balls` 按运动 ID 细分。

        用途：与页面**对账**。上游计数是秒级变动的，因此本系统同时保留
        自己推导的 `derived` 值，而不是盲目采用上游值。

        失败一律返回 None（对账属增强能力，不应影响采集）。
        """
        if self.replay:
            return None
        try:
            blob = self.client._request(
                "GET", "/v1/m/platformsSportCountPB?merchantCode=Y&enName=YBTY")
        except Exception:  # noqa: BLE001 - 对账失败不影响主流程
            return None
        if not isinstance(blob, Mapping):
            return None
        today = blob.get("0")
        if not isinstance(today, Mapping):
            return None
        sports = today.get("TY")
        if not isinstance(sports, Mapping):
            return None
        roll = sports.get("1")                 # TY=体育, "1"=滚球
        if not isinstance(roll, Mapping):
            return None
        all_ct = _to_opt_int(roll.get("ct"))
        if all_ct is None:
            return None
        sport_ct: Optional[int] = None
        balls = roll.get("balls")
        if sport_id and isinstance(balls, Mapping):
            item = balls.get(str(sport_id))
            if isinstance(item, Mapping):
                sport_ct = _to_opt_int(item.get("ct"))
        return {"sport": sport_ct, "all_sports": all_ct,
                "sport_id": str(sport_id)}

    def describe(self) -> Dict[str, Any]:
        return {
            "source": self.name,
            "kind": type(self).__name__,
            "mode": "replay" if self.replay else "online",
            "host": self.host,
            "replay": self.replay,
            "batch_size": self.batch_size,
            "session": self.session_provider.describe(),
            "session_refreshes": self.session_refreshes,
        }


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #

def make_source(
    name: Optional[str],
    *,
    saz_path: Optional[str] = None,
    host: str = DEFAULT_HOST,
    origin: str = DEFAULT_ORIGIN,
    request_id: Optional[str] = None,
    batch_size: int = 20,
) -> LEYUSource:
    """按名称构造数据源（本项目仅乐鱼）。

    乐鱼源在给出 `saz_path` 时自动进入离线回放模式——这让 CI/无网环境
    也能跑完整链路，不需要访问任何博彩域名。

    返回具体类型（而非基类 `SnapshotSource`）：下线体彩源后只剩乐鱼，
    而 `full`/`progress` 是乐鱼特有的采集参数，抽象基类不应该伪造它们。
    """
    resolved = resolve_source_name(name)
    if resolved == SOURCE_LEYU:
        return LEYUSource(
            host=host, origin=origin, request_id=request_id,
            replay=saz_path, batch_size=batch_size,
        )
    raise SourceError("不支持的数据源：%r" % resolved)
