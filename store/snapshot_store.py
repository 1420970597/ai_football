#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
存储层 —— 不可变快照 + 缓存

对应报告：
  §12.1  不可变快照（immutable snapshots）是赔率微结构分析的**正确数据模型**：
         需要还原「过程」而非「状态」。因此快照一经写入即不可修改，
         文件名内嵌时间戳，新数据写新文件，绝不覆盖。
  §4.2   快照状态语义（有效/停盘/陈旧/下架）需要额外索引支持。
  §3.4   多源冗余：同一赛事可能来自多个来源，需按来源分目录存放。

设计约束（AGENTS.md §3.2）：
  所有存储访问必须收敛在本层，不得散落在 API 或采集代码里。
  Redis 不可用时自动回退到进程内缓存，保证无外部依赖也能运行与测试。

依赖：仅标准库（Redis 通过可选导入，缺失时回退）。
"""

from __future__ import annotations

import abc
import importlib
import json
import math
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from core.models import OddsSnapshot, SnapshotState, utcnow

__all__ = [
    "SnapshotStore",
    "CacheBackend",
    "MemoryCache",
    "RedisCache",
    "make_cache",
    "safe_name",
]

_SAFE_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")


def safe_name(raw: object, max_len: int = 80) -> str:
    """把任意输入转为安全的文件名片段。

    保留中文与常见符号，其余替换为下划线；并阻止路径穿越（`../`）。
    入参声明为 `object`：调用方常直接传 match_id/league 等可能为 int 的字段，
    本函数内部会自行 str() 归一，不应要求调用方先转换。
    """
    if not isinstance(raw, str):
        raw = str(raw)
    s = _SAFE_RE.sub("_", raw.strip())
    s = s.replace("..", "_").strip("._")
    if not s:
        s = "unnamed"
    return s[:max_len]


def _finite(v: object, name: str) -> float:
    """安全浮点转换；失败或非有限值一律抛 ValueError。"""
    try:
        out = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("%s 无法转换为数值: %r" % (name, v)) from exc
    if not math.isfinite(out):
        raise ValueError("%s 非有限值: %r" % (name, v))
    return out


def _to_odds_tuple(raw: object) -> Tuple[float, ...]:
    """把 JSON 里的赔率字段转为 float 元组，非法项尽早暴露。"""
    if not isinstance(raw, (list, tuple)):
        raise TypeError("odds 应为数组，实际为 %r" % type(raw).__name__)
    return tuple(_finite(x, "odds[%d]" % i) for i, x in enumerate(raw))


# --------------------------------------------------------------------------- #
# 缓存后端
# --------------------------------------------------------------------------- #

class CacheBackend(abc.ABC):
    """缓存后端接口。

    用 abc.ABC + @abstractmethod 表达接口约束：子类未实现时**在实例化阶段**
    报错（TypeError），比运行时 raise NotImplementedError 更早暴露问题。
    """

    name = "abstract"

    @abc.abstractmethod
    def get(self, key: str) -> Optional[str]:
        """读取键；不存在或已过期返回 None。"""

    @abc.abstractmethod
    def set(self, key: str, value: str, ttl: Optional[int] = None) -> None:
        """写入键，ttl 为秒；None 表示不过期。"""

    @abc.abstractmethod
    def delete(self, key: str) -> None:
        """删除键（不存在时静默）。"""

    @abc.abstractmethod
    def keys(self, prefix: str = "") -> List[str]:
        """列举键（已过滤过期项）。"""

    @abc.abstractmethod
    def clear(self) -> None:
        """清空全部键（仅测试/运维使用）。"""

    def available(self) -> bool:
        """后端是否可用。回退实现返回 False。"""
        return True


class MemoryCache(CacheBackend):
    """进程内缓存（线程安全，带 TTL）。

    这是 Redis 不可用时的回退实现，也让测试无需外部依赖。
    """

    name = "memory"

    def __init__(self) -> None:
        self._data: Dict[str, tuple] = {}   # key -> (value, expire_at or None)
        self._lock = threading.RLock()

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            value, exp = item
            if exp is not None and time.time() > exp:
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: str, value: str, ttl: Optional[int] = None) -> None:
        exp = (time.time() + ttl) if ttl else None
        with self._lock:
            self._data[key] = (value, exp)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def keys(self, prefix: str = "") -> List[str]:
        with self._lock:
            now = time.time()
            out = []
            for k, (_, exp) in list(self._data.items()):
                if exp is not None and now > exp:
                    self._data.pop(k, None)
                    continue
                if k.startswith(prefix):
                    out.append(k)
            return sorted(out)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class RedisCache(CacheBackend):
    """Redis 缓存后端。redis 库缺失或连接失败时 available() 返回 False。

    支持**连接重试**：容器编排下 analytics-api 常先于 redis 就绪启动，
    若首次连接失败就永久回退内存，会导致缓存不跨进程。
    因此这里在构造时做有限次重试（指数退避），
    并在运行期首次成功操作时**惰性重连**。
    """

    name = "redis"

    def __init__(self, url: str, timeout: float = 2.0,
                 connect_retries: int = 5,
                 retry_backoff: float = 0.5) -> None:
        self.url = url
        self.timeout = timeout
        self.connect_retries = max(1, int(connect_retries))
        self.retry_backoff = max(0.0, float(retry_backoff))
        # 用 Any 标注：redis 为可选依赖，无法在类型层面引用其类型。
        self._client: Any = None
        self._ok = False
        self._module: Any = None

        # redis 是**可选依赖**（仅生产容器安装）。
        # 用 importlib 动态导入：类型检查器不会因未安装而报 missing-import，
        # 也无需 type: ignore（本项目开启了 warn_unused_ignores）。
        try:
            self._module = importlib.import_module("redis")
        except ImportError:
            self._module = None
            return

        self._connect()

    def _connect(self) -> None:
        """尝试建立连接，失败则按退避重试。"""
        if self._module is None:
            return
        for attempt in range(self.connect_retries):
            try:
                client = self._module.Redis.from_url(
                    self.url,
                    socket_connect_timeout=self.timeout,
                    socket_timeout=self.timeout,
                )
                client.ping()
                self._client = client
                self._ok = True
                return
            except Exception:
                self._client = None
                self._ok = False
                if attempt < self.connect_retries - 1 and self.retry_backoff > 0:
                    time.sleep(self.retry_backoff * (2 ** attempt))

    def available(self) -> bool:
        return self._ok

    def get(self, key: str) -> Optional[str]:
        if not self._ok:
            return None
        try:
            raw = self._client.get(key)
        except Exception:
            self._ok = False
            return None
        if raw is None:
            return None
        return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

    def set(self, key: str, value: str, ttl: Optional[int] = None) -> None:
        if not self._ok:
            return
        try:
            if ttl:
                self._client.setex(key, ttl, value)
            else:
                self._client.set(key, value)
        except Exception:
            self._ok = False

    def delete(self, key: str) -> None:
        if not self._ok:
            return
        try:
            self._client.delete(key)
        except Exception:
            self._ok = False

    def keys(self, prefix: str = "") -> List[str]:
        if not self._ok:
            return []
        try:
            raw = self._client.keys((prefix or "") + "*")
        except Exception:
            self._ok = False
            return []
        return sorted(
            (k.decode("utf-8") if isinstance(k, bytes) else str(k)) for k in raw
        )

    def clear(self) -> None:
        if not self._ok:
            return
        try:
            self._client.flushdb()
        except Exception:
            self._ok = False


def make_cache(url: Optional[str] = None,
               prefer_redis: bool = True) -> CacheBackend:
    """构造缓存后端：优先 Redis，失败则回退内存。

    可用环境变量：
        REDIS_URL        例如 redis://redis:6379/0
        CACHE_BACKEND    "memory" 强制使用内存（便于测试）
    """
    backend = os.environ.get("CACHE_BACKEND", "").strip().lower()
    if backend == "memory":
        return MemoryCache()

    if prefer_redis:
        target = url or os.environ.get("REDIS_URL") or "redis://localhost:6379/0"
        rc = RedisCache(target)
        if rc.available():
            return rc
    return MemoryCache()


# --------------------------------------------------------------------------- #
# 不可变快照存储
# --------------------------------------------------------------------------- #

class SnapshotStore:
    """不可变赔率快照存储（报告 §12.1）。

    目录布局：
        <root>/
          <source>/                数据来源（多源冗余）
            <league>/
              <match_id>_<home>_vs_<away>/
                <timestamp>_<market>.json     每次采集一个文件，永不覆盖
                _index.json                   该赛事的快照索引

    写入采用「临时文件 + 原子替换」以保证并发下不会读到半截 JSON。
    """

    def __init__(self, root: str | Path, cache: Optional[CacheBackend] = None) -> None:
        self.root = Path(root)
        self.cache = cache if cache is not None else make_cache()
        self._lock = threading.RLock()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError("无法创建快照根目录 %s: %s" % (self.root, exc)) from exc

    # -- 路径 ---------------------------------------------------------------

    def match_dir(self, snapshot: OddsSnapshot) -> Path:
        return (
            self.root
            / safe_name(snapshot.source or "unknown")
            / safe_name(snapshot.league or "unknown")
            / safe_name(
                "%s_%s_vs_%s"
                % (snapshot.match_id, snapshot.home, snapshot.away)
            )
        )

    # -- 写入 ---------------------------------------------------------------

    def append(self, snapshot: OddsSnapshot) -> Path:
        """追加一个快照（永不覆盖既有文件）。

        Returns:
            写入的文件路径。
        """
        d = self.match_dir(snapshot)
        with self._lock:
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise RuntimeError("无法创建快照目录 %s: %s" % (d, exc)) from exc

            ts = snapshot.captured_at.astimezone(timezone.utc).strftime(
                "%Y%m%dT%H%M%S%f"
            )
            # 加入纳秒与序号后缀，避免同微秒内多次采集相互覆盖
            target = d / ("%s_%s.json" % (ts, safe_name(snapshot.market)))
            seq = 0
            while target.exists():
                seq += 1
                target = d / (
                    "%s_%s_%d.json" % (ts, safe_name(snapshot.market), seq)
                )

            payload = snapshot.as_dict()
            payload["_schema"] = "odds_snapshot/v1"
            self._atomic_write_json(target, payload)
            self._update_index(d, target.name, payload)
        self.cache_invalidate(snapshot.match_id)
        return target

    def append_many(self, snapshots: Iterable[OddsSnapshot]) -> List[Path]:
        """批量追加，返回写入路径列表。"""
        return [self.append(s) for s in snapshots]

    @staticmethod
    def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
        """原子写入 JSON：先写临时文件，再 os.replace（同目录内原子）。"""
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=".tmp_", suffix=".json", dir=str(path.parent)
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _update_index(self, d: Path, filename: str,
                      payload: Dict[str, Any]) -> None:
        """维护赛事级别的快照索引（便于快速列举与统计）。"""
        idx_path = d / "_index.json"
        idx: Dict[str, Any] = {"files": [], "count": 0}
        if idx_path.exists():
            try:
                with idx_path.open(encoding="utf-8") as fh:
                    idx = json.load(fh)
            except (OSError, ValueError):
                idx = {"files": [], "count": 0}

        files = idx.get("files") or []
        if filename not in files:
            files.append(filename)
        idx["files"] = sorted(files)
        idx["count"] = len(idx["files"])
        idx["match_id"] = payload.get("match_id")
        idx["league"] = payload.get("league")
        idx["updated_at"] = utcnow().isoformat()
        self._atomic_write_json(idx_path, idx)

    # -- 读取 ---------------------------------------------------------------

    def load_match(self, source: str, league: str,
                   match_id: str) -> List[OddsSnapshot]:
        """读取某赛事下全部快照，按时间排序。"""
        base = self.root / safe_name(source) / safe_name(league)
        if not base.is_dir():
            return []
        out: List[OddsSnapshot] = []
        for d in sorted(base.iterdir()):
            if not d.is_dir():
                continue
            if not d.name.startswith(safe_name(match_id) + "_"):
                continue
            out.extend(self._load_dir(d))
        out.sort(key=lambda s: s.captured_at)
        return out

    def _load_dir(self, d: Path) -> List[OddsSnapshot]:
        out: List[OddsSnapshot] = []
        for f in sorted(d.glob("*.json")):
            if f.name == "_index.json":
                continue
            try:
                with f.open(encoding="utf-8") as fh:
                    payload = json.load(fh)
                out.append(self._from_payload(payload))
            except (OSError, ValueError, KeyError, TypeError):
                # 单个损坏文件不应影响整体读取
                continue
        return out

    @staticmethod
    def _from_payload(p: Dict[str, Any]) -> OddsSnapshot:
        captured = p.get("captured_at")
        try:
            ts = datetime.fromisoformat(captured) if captured else utcnow()
        except (TypeError, ValueError):
            ts = utcnow()
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        try:
            state = SnapshotState(p.get("state", "active"))
        except ValueError:
            state = SnapshotState.ACTIVE

        try:
            odds = _to_odds_tuple(p["odds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("快照赔率字段非法: %s" % exc) from exc

        return OddsSnapshot(
            match_id=str(p["match_id"]),
            league=str(p.get("league", "")),
            home=str(p.get("home", "")),
            away=str(p.get("away", "")),
            market=str(p.get("market", "1X2")),
            outcomes=tuple(str(x) for x in p["outcomes"]),
            odds=odds,
            state=state,
            captured_at=ts,
            source=str(p.get("source", "unknown")),
            metadata={"file_schema": p.get("_schema", "")},
        )

    # -- 缓存联动 -----------------------------------------------------------

    def cache_key(self, match_id: str) -> str:
        return "snapshot:match:%s" % match_id

    def cache_invalidate(self, match_id: str) -> None:
        """快照写入后使缓存失效（保证读到的是最新序列）。"""
        try:
            self.cache.delete(self.cache_key(match_id))
        except Exception:
            pass

    def cache_put(self, match_id: str, snapshots: Sequence[OddsSnapshot],
                  ttl: int = 60) -> None:
        try:
            payload = json.dumps(
                [s.as_dict() for s in snapshots], ensure_ascii=False
            )
            self.cache.set(self.cache_key(match_id), payload, ttl=ttl)
        except Exception:
            pass

    def stats(self) -> Dict[str, Any]:
        """统计快照规模（用于 /health 与运维观察）。"""
        n_files = 0
        n_matches = 0
        for d in self.root.rglob("*"):
            if d.is_dir() and (d / "_index.json").exists():
                n_matches += 1
        for f in self.root.rglob("*.json"):
            if f.name != "_index.json":
                n_files += 1
        return {
            "root": str(self.root),
            "match_dirs": n_matches,
            "snapshot_files": n_files,
            "cache_backend": self.cache.name,
            "cache_available": self.cache.available(),
        }
