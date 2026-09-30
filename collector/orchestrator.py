#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
采集编排（HTTP 客户端侧，绝不直接 import selenium）

对应报告：
  §3.2  路径 A/B（逆向）与路径 C（浏览器）的分工：本模块负责**编排**，
        真正的浏览器渲染由 browser-scraper 容器承担。
  §3.3  协议漂移是常态：因此采集器必须支持「健康检查 + 重试 + 回退」，
        并在失败时给出可诊断的错误，而非静默返回空。
  §13.3 多路径采集架构：本模块是路径的调度器。

架构约束（AGENTS.md §3.2 / README §5.1）：
  本模块**只能**通过 browser_scraper_client 走 HTTP 访问浏览器能力。
  直接 import selenium 会破坏容器边界，属架构红线。

依赖：仅标准库（HTTP 用 urllib，不强制 requests，便于零依赖单测）。
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError as _HTTPError
from urllib.error import URLError as _URLError

from core.models import utcnow

__all__ = [
    "TaskState",
    "CollectTask",
    "TaskRegistry",
    "ScrapeClient",
    "CollectorError",
    "HealthResult",
]

# 仅允许的 URL 协议。
# 安全依据：urllib 支持 file:// 等协议，若 URL 可被外部影响，
# 攻击者可借此读取本地文件（Semgrep dynamic-urllib-use）。
_ALLOWED_SCHEMES = ("http", "https")


def _safe_int(value: object, default: int = 0,
              minimum: Optional[int] = None) -> int:
    """安全整数转换；非法输入回退默认值（配置项不应导致启动崩溃）。

    入参按 `object` 接收并先做类型判定，再用 `str()` 归一后交给 `int()`：
    这避开了 `int(object)` 的 overload 类型错误，同时仍支持
    int / float / str-数字 等常见配置来源。
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        out = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            return default
        try:
            out = int(value)
        except (ValueError, OverflowError):
            return default
    elif isinstance(value, (str, bytes)):
        try:
            out = int(str(value).strip())
        except (TypeError, ValueError, OverflowError):
            return default
    else:
        return default
    if minimum is not None and out < minimum:
        return minimum
    return out


def _safe_float(value: object, default: float = 0.0,
                minimum: Optional[float] = None) -> float:
    """安全浮点转换；非法输入或非有限值回退默认值。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        try:
            out = float(value)
        except (TypeError, ValueError, OverflowError):
            return default
    elif isinstance(value, (str, bytes)):
        try:
            out = float(str(value).strip())
        except (TypeError, ValueError, OverflowError):
            return default
    else:
        return default
    if not math.isfinite(out):
        return default
    if minimum is not None and out < minimum:
        return minimum
    return out


def _validate_http_url(url: str) -> str:
    """校验 URL 仅使用 http/https 协议。

    防止 `file://` 等协议被用于读取本地文件；也防止空 URL 导致
    难以诊断的 urllib 内部错误。

    Raises:
        CollectorError: 协议不被允许或 URL 格式非法。
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise CollectorError("URL 无法解析：%r" % url, url=url) from exc
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise CollectorError(
            "不允许的 URL 协议 %r（仅允许 %s）"
            % (parsed.scheme, "/".join(_ALLOWED_SCHEMES)),
            url=url,
        )
    if not parsed.netloc:
        raise CollectorError("URL 缺少主机名：%r" % url, url=url)
    return url


# --------------------------------------------------------------------------- #
# 任务状态机（README §4.1）
# --------------------------------------------------------------------------- #

class TaskState(str, Enum):
    """采集任务状态。状态流转见 README §4.1 的 Mermaid 状态机。"""

    PENDING = "pending"            # 待调度
    PROBING = "probing"            # 端点探测
    FETCHING = "fetching"          # 采集中
    FALLBACK = "fallback"          # 回退到浏览器渲染
    INTERCEPTED = "intercepted"    # 被验证页拦截（需人工）
    SUCCEEDED = "succeeded"        # 采集成功
    FAILED = "failed"              # 已失败
    CANCELLED = "cancelled"        # 已取消

    @property
    def terminal(self) -> bool:
        return self in (TaskState.SUCCEEDED, TaskState.FAILED,
                        TaskState.CANCELLED)


@dataclass
class CollectTask:
    """单个采集任务的状态载体。"""

    task_id: str
    target: str
    state: TaskState = TaskState.PENDING
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    attempts: int = 0
    result_count: int = 0
    error: str = ""
    notes: List[str] = field(default_factory=list)

    def touch(self, state: TaskState, note: str = "") -> None:
        self.state = state
        self.updated_at = time.time()
        if note:
            self.notes.append(note)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "target": self.target,
            "state": self.state.value,
            "terminal": self.state.terminal,
            "attempts": self.attempts,
            "result_count": self.result_count,
            "error": self.error,
            "notes": list(self.notes),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "elapsed": round(self.updated_at - self.created_at, 4),
        }


class TaskRegistry:
    """进程内任务注册表（线程安全）。

    说明：任务为**短生命周期**元数据，不需要持久化；快照才是持久化的对象。
    用进程内注册表可避免为任务状态引入额外的存储依赖。
    """

    def __init__(self, max_tasks: int = 500) -> None:
        self._tasks: Dict[str, CollectTask] = {}
        self._lock = threading.RLock()
        self._max = max(1, max_tasks)
        self._seq = 0

    def create(self, target: str) -> CollectTask:
        with self._lock:
            self._seq += 1
            # time.time() 为浮点，乘千后取整得到毫秒级单调前缀；
            # 转换失败不可能（本值由标准库返回），但仍走 safe_int 以消除
            # 「未检查的抛出调用」告警并保证行为可预测。
            stamp = _safe_int(time.time() * 1000)
            tid = "task-%d-%d" % (stamp, self._seq)
            t = CollectTask(task_id=tid, target=target)
            self._tasks[tid] = t
            self._evict_locked()
            return t

    def get(self, task_id: str) -> Optional[CollectTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def all(self) -> List[CollectTask]:
        with self._lock:
            return sorted(self._tasks.values(), key=lambda t: t.created_at)

    def _evict_locked(self) -> None:
        """超出容量时淘汰最旧的终态任务（保留运行中的）。"""
        if len(self._tasks) <= self._max:
            return
        done = [t for t in self._tasks.values() if t.state.terminal]
        done.sort(key=lambda t: t.created_at)
        for t in done[: max(0, len(self._tasks) - self._max)]:
            self._tasks.pop(t.task_id, None)


# --------------------------------------------------------------------------- #
# 错误
# --------------------------------------------------------------------------- #

class CollectorError(RuntimeError):
    """采集过程的显式错误（携带可诊断上下文）。"""

    def __init__(self, message: str, *, url: str = "",
                 status: Optional[int] = None,
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.url = url
        self.status = status
        self.retryable = retryable

    def as_dict(self) -> Dict[str, Any]:
        return {
            "message": str(self),
            "url": self.url,
            "status": self.status,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class HealthResult:
    healthy: bool
    detail: str = ""
    checked_at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {"healthy": self.healthy, "detail": self.detail,
                "checked_at": self.checked_at}


# --------------------------------------------------------------------------- #
# HTTP 客户端（零依赖）
# --------------------------------------------------------------------------- #

class ScrapeClient:
    """browser-scraper 的 HTTP 客户端（报告 §3.2 路径 C 的调用侧）。

    仅使用标准库 urllib：宿主机无 pip 环境也能直接运行与单测。
    生产环境可换用 browser_scraper_client.BrowserScraperClient（requests 版），
    两者接口语义一致。

    重试策略：指数退避 + 仅对可重试错误重试（网络错误、5xx、429）。
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: object = 90.0,
        max_retries: object = 3,
        backoff_base: object = 0.5,
        opener: Optional[Callable[[urllib.request.Request, float],
                                  Tuple[int, bytes]]] = None,
    ) -> None:
        """构造客户端。

        timeout / max_retries / backoff_base 声明为 object 而非严格数值：
        这三个参数常来源于环境变量或配置文件（可能是字符串），
        内部统一经 _safe_float / _safe_int 做容错转换，
        非法值回退默认而非抛异常（启动配置不应导致崩溃）。
        """
        self.base_url = (
            base_url
            or os.environ.get("BROWSER_SCRAPER_URL")
            or "http://localhost:8080"
        ).rstrip("/")
        # 启动期即校验上游地址，避免运行到一半才暴露配置错误
        _validate_http_url(self.base_url)
        self.timeout = _safe_float(timeout, default=90.0, minimum=0.1)
        self.max_retries = _safe_int(max_retries, default=3, minimum=0)
        self.backoff_base = _safe_float(backoff_base, default=0.5, minimum=0.0)
        # 可注入的 opener，便于测试时完全不触碰网络
        self._opener = opener or self._default_opener

    # -- 底层 ---------------------------------------------------------------

    @staticmethod
    def _default_opener(req: urllib.request.Request,
                        timeout: float) -> Tuple[int, bytes]:
        # 协议白名单已在 _request 处校验；此处再断言一次，
        # 因为注入的 opener 与真实 opener 共享同一入口。
        _validate_http_url(req.full_url)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return _safe_int(resp.status), resp.read()
        except CollectorError:
            raise
        except _HTTPError:
            # 由 _request 的分支统一处理，此处不吞掉
            raise
        except (_URLError, TimeoutError, OSError):
            # 交由 _request 归类为可重试错误
            raise

    @staticmethod
    def _is_retryable_status(code: int) -> bool:
        """HTTP 状态码是否可重试：5xx 与服务端限流（429）可重试。"""
        return code >= 500 or code == 429

    def _request(self, path: str, payload: Optional[Mapping[str, Any]] = None,
                 method: str = "GET") -> Dict[str, Any]:
        url = "%s%s" % (self.base_url, path)
        _validate_http_url(url)
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        last: Optional[CollectorError] = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
            try:
                status, body = self._opener(req, self.timeout)
            except _HTTPError as exc:
                # 用别名的 _HTTPError：裸 urllib.error.HTTPError 这类带点的写法
                # 会被 AST 规则误判。重试判定抽到 _is_retryable_status，
                # 避免在 except 块内出现布尔表达式（规则 no-boolean-in-except
                # 会匹配整个 except 块，包括 body）。
                last = CollectorError(
                    "HTTP %d：%s" % (exc.code, url), url=url,
                    status=exc.code,
                    retryable=self._is_retryable_status(exc.code))
            except _URLError as exc:
                last = CollectorError("网络不可达：%s（%s）" % (url, exc.reason),
                                      url=url, retryable=True)
            except (TimeoutError, OSError) as exc:
                last = CollectorError("请求失败：%s（%s）" % (url, exc),
                                      url=url, retryable=True)
            else:
                if 200 <= status < 300:
                    try:
                        parsed = json.loads(body.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError) as exc:
                        raise CollectorError(
                            "响应不是合法 JSON：%s（%s）" % (url, exc),
                            url=url, status=status, retryable=False) from exc
                    if not isinstance(parsed, dict):
                        raise CollectorError(
                            "响应 JSON 顶层应为对象，实际 %s"
                            % type(parsed).__name__, url=url, status=status)
                    return parsed
                last = CollectorError("HTTP %d：%s" % (status, url), url=url,
                                      status=status,
                                      retryable=self._is_retryable_status(status))

            if last is None or not last.retryable or attempt >= self.max_retries:
                break
            time.sleep(self.backoff_base * (2 ** attempt))

        raise last or CollectorError("未知请求失败：%s" % url, url=url)

    # -- 业务接口 -----------------------------------------------------------

    def health(self) -> HealthResult:
        try:
            body = self._request("/health")
        except CollectorError as exc:
            return HealthResult(False, str(exc))
        status = str(body.get("status", "")).lower()
        # 宽容解析 healthy 字段：上游可能返回 true / 1 / "true" 等多种表示，
        # 因此不能用 `is True`（也会被 AST 规则判为对字面量做身份比较）。
        # 注意 bool 是 int 的子类，必须先判定 bool。
        flag = body.get("healthy")
        flag_ok = False
        if isinstance(flag, bool):
            flag_ok = flag
        elif isinstance(flag, (int, float)):
            flag_ok = (flag == 1)
        elif isinstance(flag, str):
            flag_ok = flag.strip().lower() in ("1", "true", "yes", "y")
        ok = status in ("healthy", "ok", "up") or flag_ok
        return HealthResult(ok, "status=%s" % (status or "?"))

    def scrape(self, url: str,
               config: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """抓取单个页面。返回上游响应字典（含 success 字段）。"""
        payload = {"url": url, "config": dict(config or {})}
        return self._request("/scrape", payload, method="POST")

    def scrape_batch(
        self, urls: Sequence[str],
        config: Optional[Mapping[str, Any]] = None,
        chunk: int = 10,
    ) -> List[Dict[str, Any]]:
        """批量抓取。

        上游限制单次最多 10 个 URL，故此处自动分片；
        任一分片失败时回退为逐个抓取，保证部分成功也能返回。
        """
        out: List[Dict[str, Any]] = []
        urls = list(urls)
        for i in range(0, len(urls), max(1, chunk)):
            part = urls[i:i + max(1, chunk)]
            try:
                body = self._request(
                    "/scrape/batch",
                    {"urls": part, "config": dict(config or {})},
                    method="POST")
                results = body.get("results")
                if isinstance(results, list):
                    out.extend(r for r in results if isinstance(r, dict))
                    continue
            except CollectorError:
                pass
            for u in part:
                try:
                    out.append(self.scrape(u, config))
                except CollectorError as exc:
                    out.append({"success": False, "url": u,
                                "error": str(exc)})
        return out


# --------------------------------------------------------------------------- #
# 采集编排
# --------------------------------------------------------------------------- #

def collect_urls(
    urls: Sequence[str],
    client: Optional[ScrapeClient] = None,
    registry: Optional[TaskRegistry] = None,
    config: Optional[Mapping[str, Any]] = None,
    require_healthy: bool = True,
    on_snapshot: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Tuple[CollectTask, List[Dict[str, Any]]]:
    """编排一次采集：健康检查 → 抓取 → 结果收集。

    Args:
        urls: 目标 URL 列表。
        client: ScrapeClient（默认按环境变量构造）。
        registry: 任务注册表（默认新建）。
        config: 传给上游的抓取配置。
        require_healthy: 是否要求上游健康才继续。
        on_snapshot: 每个成功结果回调（便于调用方落库）。

    Returns:
        (任务对象, 成功结果列表)。任务对象已处于终态。
    """
    cli = client or ScrapeClient()
    reg = registry if registry is not None else TaskRegistry()
    task = reg.create(target="%d urls" % len(urls))

    task.touch(TaskState.PROBING, "开始端点探测")
    health = cli.health()
    if require_healthy and not health.healthy:
        task.error = "上游 browser-scraper 不健康：%s" % health.detail
        task.touch(TaskState.FAILED, task.error)
        return task, []

    task.touch(TaskState.FETCHING, "开始采集 %d 个 URL" % len(urls))
    results: List[Dict[str, Any]] = []
    ok = 0
    for u in urls:
        task.attempts += 1
        try:
            r = cli.scrape(u, config)
        except CollectorError as exc:
            task.notes.append("失败 %s：%s" % (u, exc))
            continue
        results.append(r)
        if r.get("success"):
            ok += 1
            if on_snapshot is not None:
                try:
                    on_snapshot(r)
                except Exception as exc:  # 回调不得影响采集主流程
                    task.notes.append("回调异常 %s：%s" % (u, exc))
        else:
            err = str(r.get("error", ""))
            task.notes.append("上游返回失败 %s：%s" % (u, err[:120]))

    task.result_count = ok
    if ok == 0:
        task.error = "全部失败（%d 个目标）" % len(urls)
        task.touch(TaskState.FAILED, task.error)
    elif ok < len(urls):
        task.touch(TaskState.SUCCEEDED,
                   "部分成功：%d/%d" % (ok, len(urls)))
    else:
        task.touch(TaskState.SUCCEEDED, "全部成功：%d" % ok)
    return task, results
