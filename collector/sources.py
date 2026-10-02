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
      ├── TicaiFileSource   体彩官方 API 落盘 JSON（场次*.json）
      └── LeYuSource        乐鱼 API（在线 HTTP / saz 离线回放）

`LeYuSource` 内部复用 `LeYuClient`（HTTP）与 `leyu_ws`（实时推送），
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
import zipfile
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from core.models import OddsSnapshot, utcnow

from .leyu_client import (
    DEFAULT_HOST,
    DEFAULT_ORIGIN,
    AuthError,
    LeYuClient,
    LeYuMatch,
    decode_envelope,
    parse_match_list,
    parse_odds_block,
)
from .session import (
    Session,
    SessionError,
    SessionProvider,
    make_session_provider,
)
from .leyu_normalizer import DEFAULT_SOURCE as LEYU_DISPLAY_SOURCE
from .leyu_normalizer import DEFAULT_SOURCE as LEYU_DISPLAY_SOURCE
from .leyu_normalizer import (
    DEFAULT_STALE_AFTER,
    snapshots_from_match,
    normalize_leyu_matches,
)

#: 体彩源写入快照的展示名（与 service.DEFAULT_SOURCE 保持一致）
TICAI_DISPLAY_SOURCE = "体彩官方API"

__all__ = [
    "SOURCE_LEYU",
    "SOURCE_TICAI",
    "SOCCER_SPORT_ID",
    "KNOWN_SOURCES",
    "SourceError",
    "SnapshotSource",
    "TicaiFileSource",
    "LeYuSource",
    "resolve_source_name",
    "make_source",
]

#: 数据源标识
SOURCE_LEYU = "leyu"
SOURCE_TICAI = "ticai"

#: 足球运动 ID（乐鱼 `spList` 中 csid="1"）。
#: 本项目是足球估值系统；同一网关还返回篮球/网球/乒乓球等，必须默认过滤。
SOCCER_SPORT_ID = "1"

KNOWN_SOURCES: Tuple[str, ...] = (SOURCE_LEYU, SOURCE_TICAI)

#: 别名 → 规范名（兼容中文标识与历史写法）
_SOURCE_ALIASES: Mapping[str, str] = {
    "leyu": SOURCE_LEYU,
    "乐鱼": SOURCE_LEYU,
    "乐鱼api": SOURCE_LEYU,
    "乐鱼官方api": SOURCE_LEYU,
    "ticai": SOURCE_TICAI,
    "体彩": SOURCE_TICAI,
    "体彩官方api": SOURCE_TICAI,
    "sporttery": SOURCE_TICAI,
}


class SourceError(Exception):
    """数据源读取/解析失败。"""


def _to_int(value: object, default: int = 0) -> int:
    """宽松整数转换（配置/入参可能来自环境变量字符串）。"""
    try:
        return int(str(value))
    except (TypeError, ValueError, OverflowError):
        return default


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

        返回 `LeYuMatch` 列表；不支持赛程列举的数据源（如本地文件源）
        返回空列表。后台实时推送依赖它来选取订阅目标，因此必须在基类
        声明，而不是调用处用 `getattr` 绕过类型检查。
        """
        return []

    def describe(self) -> Dict[str, Any]:
        """数据源元信息（供 /health 与排障使用）。"""
        return {"source": self.name, "kind": type(self).__name__}


# --------------------------------------------------------------------------- #
# 体彩源（保留：数据源无关的证明 + 回归对照）
# --------------------------------------------------------------------------- #

class TicaiFileSource(SnapshotSource):
    """体彩官方 API 落盘 JSON（`场次*.json`）→ 快照。

    这是切换前的默认源，现保留为可选后端：
    既保证原有 500 例测试与历史数据仍可用，也作为「数据源无关」的对照实现。
    """

    name = SOURCE_TICAI
    display_source = TICAI_DISPLAY_SOURCE

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def load_records(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for p in sorted(self.root.glob("场次*.json")):
            try:
                with p.open(encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                out.append(data)
            elif isinstance(data, list):
                out.extend(x for x in data if isinstance(x, dict))
        return out

    def fetch(
        self,
        mids: Optional[Sequence[str]] = None,
        max_matches: Optional[int] = None,
        stale_after: float = DEFAULT_STALE_AFTER,
        captured: Optional[datetime] = None,
    ) -> Tuple[List[OddsSnapshot], List[str]]:
        # 延迟导入：normalizer 同时导出 legacy 名称，避免循环导入
        from .normalizer import normalize_matches

        records = self.load_records()
        if mids:
            wanted = {str(m) for m in mids}
            records = [
                r for r in records
                if str((r.get("基本信息") or {}).get("场次号")
                       or (r.get("基本信息") or {}).get("比赛ID") or "") in wanted
            ]
        if max_matches is not None:
            records = records[: max(0, _to_int(max_matches))]
        res = normalize_matches(
            records,
            source=self.display_source,
            default_time=captured or utcnow(),
        )
        issues = ["%s/%s: %s" % (i.match_id, i.field, i.detail) for i in res.issues]
        return res.snapshots, issues

    def describe(self) -> Dict[str, Any]:
        return {
            "source": self.name,
            "kind": type(self).__name__,
            "root": str(self.root),
            "exists": self.root.exists(),
        }


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


class LeYuSource(SnapshotSource):
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
        self._client: Optional[LeYuClient] = None
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
    def client(self) -> LeYuClient:
        if self._client is None:
            sess = self.session
            self._client = LeYuClient(
                host=self.host, origin=self.origin,
                request_id=sess.request_id,
                cuid=self.cuid_override or sess.cuid or None,
                timeout=self.timeout,
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

    def schedule(self, replay: bool = False) -> List[LeYuMatch]:
        """阶段 1：全量赛程（不含赔率）。"""
        if replay or self.replay:
            return parse_match_list(self._replay_blob(self.replay_sids[0]))
        return self._call_with_refresh(lambda: self.client.all_matches())

    def odds(
        self, mids: Sequence[str], replay: bool = False,
        progress: Optional[Any] = None,
    ) -> List[LeYuMatch]:
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
        out: List[LeYuMatch] = []
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
            # full=True 时不做截断；显式给了 max_matches 则仍以其为准
            effective_max = max_matches if max_matches is not None else (
                None if full else 60)
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
        schedule: Sequence[LeYuMatch],
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
    corpus_root: Optional[str | Path] = None,
    saz_path: Optional[str] = None,
    host: str = DEFAULT_HOST,
    origin: str = DEFAULT_ORIGIN,
    request_id: Optional[str] = None,
    batch_size: int = 20,
) -> SnapshotSource:
    """按名称构造数据源（默认乐鱼）。

    乐鱼源在给出 `saz_path` 时自动进入离线回放模式——这让 CI/无网环境
    也能跑完整链路，不需要访问任何博彩域名。
    """
    resolved = resolve_source_name(name)
    if resolved == SOURCE_LEYU:
        return LeYuSource(
            host=host, origin=origin, request_id=request_id,
            replay=saz_path, batch_size=batch_size,
        )
    if corpus_root is None:
        raise SourceError("体彩源需要 corpus_root（场次*.json 所在目录）")
    return TicaiFileSource(corpus_root)
