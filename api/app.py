#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
REST 接入层（B/S 架构的 Server 端入口）

契约见 README §6。设计约束（AGENTS.md §3.2）：
  - 本层**只做** HTTP 编解码、入参校验与上下文装配
  - 业务逻辑一律委托 service.ValuationService
  - 存储访问一律经 service（不在此处直接触 SnapshotStore）

端点（11 个）：
  GET  /health
  GET  /api/v1/matches
  GET  /api/v1/matches/<id>
  GET  /api/v1/odds/<id>
  GET  /api/v1/markets                         玩法目录
  GET  /api/v1/markets/<id>                    某场全部玩法统计
  GET  /api/v1/markets/<id>/<market>           单玩法明细
  GET  /api/v1/model/<id>?market=HAD           Poisson 模型概率
  GET  /api/v1/consistency/<id>                跨玩法边际一致性
  GET  /api/v1/fair/<id>
  GET  /api/v1/edge/<id>
  GET  /api/v1/microstructure/<id>
  POST /api/v1/portfolio
  GET  /api/v1/calibration
  POST /api/v1/collect
  GET  /api/v1/tasks/<task_id>

实现说明：为保持核心层零依赖，本模块用标准库 http.server 实现（不引入 Flask）。
生产可平移到 Flask/FastAPI —— 路由处理函数与框架无关，仅需替换适配壳。
"""

from __future__ import annotations

import json
import re
import math
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from collector.orchestrator import TaskRegistry
from service.valuation import ValuationService

__all__ = ["create_app", "ApiApp", "run_server"]

API_PREFIX = "/api/v1"
MAX_BODY_BYTES = 64 * 1024


# --------------------------------------------------------------------------- #
# 入参校验工具
# --------------------------------------------------------------------------- #

class BadRequest(ValueError):
    """入参非法（对应 400）。"""


class NotFound(LookupError):
    """资源不存在（对应 404）。"""


def _q1(query: Mapping[str, List[str]], name: str,
        default: Optional[str] = None) -> Optional[str]:
    """取查询参数的首个值。"""
    vals = query.get(name)
    if not vals:
        return default
    return vals[0]


def _q_int(query: Mapping[str, List[str]], name: str, default: int,
           minimum: Optional[int] = None,
           maximum: Optional[int] = None) -> int:
    raw = _q1(query, name)
    if raw is None or raw == "":
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError) as exc:
        raise BadRequest("参数 %s 必须是整数，实际 %r" % (name, raw)) from exc
    if minimum is not None and val < minimum:
        raise BadRequest("参数 %s 不能小于 %d" % (name, minimum))
    if maximum is not None and val > maximum:
        raise BadRequest("参数 %s 不能大于 %d" % (name, maximum))
    return val


def _q_float(query: Mapping[str, List[str]], name: str, default: float,
             minimum: Optional[float] = None,
             maximum: Optional[float] = None) -> float:
    raw = _q1(query, name)
    if raw is None or raw == "":
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError) as exc:
        raise BadRequest("参数 %s 必须是数值，实际 %r" % (name, raw)) from exc
    if not math.isfinite(val):
        raise BadRequest("参数 %s 必须是有限值" % name)
    if minimum is not None and val < minimum:
        raise BadRequest("参数 %s 不能小于 %s" % (name, minimum))
    if maximum is not None and val > maximum:
        raise BadRequest("参数 %s 不能大于 %s" % (name, maximum))
    return val


def _body_float(body: Mapping[str, Any], name: str, default: float,
                minimum: Optional[float] = None,
                maximum: Optional[float] = None) -> float:
    """从请求体提取数值字段（可选），带类型与区间校验。

    不使用裸 float()：请求体是外部输入，转换失败必须变成 400 而非 500。
    """
    if name not in body or body[name] is None:
        return default
    raw = body[name]
    if isinstance(raw, bool):
        raise BadRequest("%s 不能是布尔值" % name)
    if isinstance(raw, (int, float)):
        # raw 已确认为数值类型；仍包一层以防 Decimal/自定义数值子类转换异常
        try:
            val = float(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise BadRequest("字段 %s 无法转为数值: %r" % (name, raw)) from exc
    elif isinstance(raw, str):
        try:
            val = float(raw.strip())
        except (TypeError, ValueError) as exc:
            raise BadRequest("字段 %s 不是数值: %r" % (name, raw)) from exc
    else:
        raise BadRequest("字段 %s 类型不支持: %s" % (name, type(raw).__name__))
    if not math.isfinite(val):
        raise BadRequest("字段 %s 必须是有限值" % name)
    if minimum is not None and val < minimum:
        raise BadRequest("字段 %s 不能小于 %s" % (name, minimum))
    if maximum is not None and val > maximum:
        raise BadRequest("字段 %s 不能大于 %s" % (name, maximum))
    return val


def _body_probs(body: Mapping[str, Any], name: str) -> Optional[List[float]]:
    """从请求体提取概率序列（可选字段）。"""
    if name not in body or body[name] is None:
        return None
    raw = body[name]
    if not isinstance(raw, (list, tuple)):
        raise BadRequest("%s 应为数值数组" % name)
    out: List[float] = []
    for i, v in enumerate(raw):
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            raise BadRequest("%s[%d] 类型不支持" % (name, i))
        try:
            x = float(v)
        except (TypeError, ValueError) as exc:
            raise BadRequest("%s[%d] 不是数值: %r" % (name, i, v)) from exc
        if not math.isfinite(x) or x < 0.0 or x > 1.0:
            raise BadRequest("%s[%d] 必须在 [0,1] 内" % (name, i))
        out.append(x)
    return out


# --------------------------------------------------------------------------- #
# 应用（与 HTTP 框架解耦的路由层）
# --------------------------------------------------------------------------- #

_PLACEHOLDER_RE = re.compile(r"^<[A-Za-z_][A-Za-z0-9_]*>$")


def _is_placeholder(segment: str) -> bool:
    """判断路径模板片段是否为占位符（如 <id> / <market>）。"""
    return bool(_PLACEHOLDER_RE.match(segment))


class ApiApp:
    """路由与处理逻辑（不含 socket 细节，便于单测直接调用）。"""

    def __init__(self, service: ValuationService,
                 registry: Optional[TaskRegistry] = None) -> None:
        self.svc = service
        self.registry = registry if registry is not None else TaskRegistry()

    # -- 分发 ---------------------------------------------------------------

    def dispatch(self, method: str, path: str,
                 query: Mapping[str, List[str]],
                 body: Mapping[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """把请求分发到处理函数，返回 (HTTP 状态码, 响应体)。"""
        routes: List[Tuple[str, str, Callable[..., Dict[str, Any]]]] = [
            ("GET", "/health", self.h_health),
            ("GET", "/matches", self.h_matches),
            ("GET", "/matches/<id>", self.h_match_detail),
            ("GET", "/odds/<id>", self.h_odds),
            ("GET", "/markets", self.h_market_catalog),
            ("GET", "/markets/<id>", self.h_markets),
            ("GET", "/markets/<id>/<market>", self.h_market_detail),
            ("GET", "/model/<id>", self.h_model),
            ("GET", "/consistency/<id>", self.h_consistency),
            ("GET", "/fair/<id>", self.h_fair),
            ("GET", "/edge/<id>", self.h_edge),
            ("GET", "/microstructure/<id>", self.h_microstructure),
            ("POST", "/portfolio", self.h_portfolio),
            ("GET", "/calibration", self.h_calibration),
            ("POST", "/collect", self.h_collect),
            ("GET", "/tasks/<id>", self.h_task),
        ]

        norm = path.rstrip("/") or "/"
        if norm == "/health":
            if method != "GET":
                return HTTPStatus.METHOD_NOT_ALLOWED, {"error": "仅支持 GET"}
            return 200, self.h_health(query, body)

        if not norm.startswith(API_PREFIX):
            return HTTPStatus.NOT_FOUND, {
                "error": "未知路径", "path": path,
                "hint": "API 前缀为 %s" % API_PREFIX,
            }
        sub = norm[len(API_PREFIX):] or "/"
        parts = [unquote(p) for p in sub.strip("/").split("/") if p]

        try:
            for m, pattern, handler in routes:
                if m != method:
                    continue
                pat_parts = [p for p in pattern.strip("/").split("/") if p]
                if len(pat_parts) != len(parts):
                    continue
                args: List[str] = []
                matched = True
                for pp, ap in zip(pat_parts, parts):
                    # 通用占位符 <name>（此前只认死字面量 "<id>"，
                    # 导致 /markets/<id>/<market> 这类多段路径永远匹配不上）
                    if _is_placeholder(pp):
                        args.append(ap)
                    elif pp != ap:
                        matched = False
                        break
                if matched:
                    return 200, handler(query, body, *args)

            # 路径存在但方法不匹配 → 405
            for m, pattern, _h in routes:
                pat_parts = [p for p in pattern.strip("/").split("/") if p]
                if len(pat_parts) != len(parts):
                    continue
                if all(_is_placeholder(pp) or pp == ap
                       for pp, ap in zip(pat_parts, parts)):
                    return HTTPStatus.METHOD_NOT_ALLOWED, {
                        "error": "方法不允许", "allowed": m, "path": path,
                    }
            return HTTPStatus.NOT_FOUND, {"error": "未知端点", "path": path}
        except BadRequest as exc:
            return HTTPStatus.BAD_REQUEST, {"error": str(exc)}
        except NotFound as exc:
            return HTTPStatus.NOT_FOUND, {"error": str(exc)}
        except ValueError as exc:
            return HTTPStatus.BAD_REQUEST, {"error": str(exc)}
        except Exception as exc:  # 兜底：绝不把堆栈泄露给客户端
            return HTTPStatus.INTERNAL_SERVER_ERROR, {
                "error": "内部错误", "detail": str(exc)[:200],
            }

    # -- 处理函数 -----------------------------------------------------------

    def h_health(self, query: Mapping[str, List[str]],
                 body: Mapping[str, Any], *_a: str) -> Dict[str, Any]:
        return self.svc.health()

    def h_matches(self, query: Mapping[str, List[str]],
                  body: Mapping[str, Any], *_a: str) -> Dict[str, Any]:
        items = self.svc.list_matches(
            date=_q1(query, "date"),
            league=_q1(query, "league"),
            query=_q1(query, "q"),
        )
        return {"count": len(items), "matches": items}

    def h_match_detail(self, query: Mapping[str, List[str]],
                       body: Mapping[str, Any], match_id: str) -> Dict[str, Any]:
        d = self.svc.get_match(match_id)
        if d is None:
            raise NotFound("赛事不存在：%s" % match_id)
        return d

    def h_odds(self, query: Mapping[str, List[str]],
               body: Mapping[str, Any], match_id: str) -> Dict[str, Any]:
        d = self.svc.get_odds(match_id)
        if d is None:
            raise NotFound("无赔率快照：%s" % match_id)
        return d

    # -- 多玩法 -------------------------------------------------------------

    def h_market_catalog(self, query: Mapping[str, List[str]],
                         body: Mapping[str, Any]) -> Dict[str, Any]:
        """玩法目录：本项目支持的全部玩法及其结果空间。"""
        return self.svc.market_catalog()

    def h_markets(self, query: Mapping[str, List[str]],
                  body: Mapping[str, Any], match_id: str) -> Dict[str, Any]:
        """某场赛事的**全部玩法**统计（去水 / 水钱 / EV / 方法分歧）。"""
        d = self.svc.list_markets(match_id)
        if d is None:
            raise NotFound("无该赛事样例：%s" % match_id)
        return d

    def h_market_detail(self, query: Mapping[str, List[str]],
                        body: Mapping[str, Any], match_id: str,
                        market: str) -> Dict[str, Any]:
        """单个玩法的逐结果明细（赔率 / 隐含 / 公平概率 / 公平赔率）。"""
        d = self.svc.get_market(match_id, market)
        if d is None:
            # 区分「赛事不存在」与「该玩法未落库」——两者处置完全不同：
            # 前者是请求错误，后者是**已知数据缺口**，需要重新采集而非重试。
            known = self.svc.list_markets(match_id)
            if known is None:
                raise NotFound("无该赛事：%s" % match_id)
            raise NotFound(
                "该场未落库玩法 %s（现有玩法：%s）。"
                "注意：既有 output JSON 仅保存 HAD/HHAD 赔率，"
                "TTG/CRS/HAFU 虽标记 Selling 但赔率未采集——"
                "这是已知数据缺口，需重新采集（见 collector."
                "normalizer.parse_pooled_odds），重试本请求无济于事。"
                % (market, ", ".join(m["market"] for m in known["markets"]))
            )
        return d

    def h_model(self, query: Mapping[str, List[str]],
                body: Mapping[str, Any], match_id: str) -> Dict[str, Any]:
        """Poisson 模型概率（独立于市场定价的 p_model）。

        这是「真实 edge」判定的必要输入：
        与 /fair 的市场隐含概率相比，模型概率才是独立的第二意见。
        """
        d = self.svc.get_model_probs(
            match_id,
            market=_q1(query, "market", "HAD") or "HAD",
            shrink=_q_float(query, "shrink", 0.3, minimum=0.0, maximum=1.0),
            league_avg_goals=_q_float(query, "league_avg_goals", 2.6,
                                      minimum=0.1),
        )
        if d is None:
            raise NotFound(
                "无进球统计数据，无法建模：%s（该场缺少 详细分析数据.数据统计）"
                % match_id)
        if "error" in d:
            raise BadRequest(d["error"])
        return d

    def h_consistency(self, query: Mapping[str, List[str]],
                      body: Mapping[str, Any],
                      match_id: str) -> Dict[str, Any]:
        """跨玩法边际一致性校验（数据错误 vs 真实套利检测）。"""
        d = self.svc.cross_market_check(
            match_id,
            tol=_q_float(query, "tol", 0.02, minimum=0.0, maximum=1.0),
        )
        if d is None:
            raise NotFound("无该赛事样例：%s" % match_id)
        return d

    def h_fair(self, query: Mapping[str, List[str]],
               body: Mapping[str, Any], match_id: str) -> Dict[str, Any]:
        d = self.svc.get_fair(
            match_id,
            market=_q1(query, "market", "1X2") or "1X2",
            method=_q1(query, "method", "auto") or "auto",
        )
        if d is None:
            raise NotFound("无该市场快照：%s" % match_id)
        return d

    def h_edge(self, query: Mapping[str, List[str]],
               body: Mapping[str, Any], match_id: str) -> Dict[str, Any]:
        d = self.svc.get_edge(
            match_id,
            market=_q1(query, "market", "1X2") or "1X2",
            method=_q1(query, "method", "auto") or "auto",
            n_obs=_q_int(query, "n_obs", 0, minimum=0),
            sigma_p=_q_float(query, "sigma_p", 0.0, minimum=0.0) or None,
            q_fill=_q_float(query, "q_fill", 1.0, minimum=0.0, maximum=1.0),
            cost_exec=_q_float(query, "cost_exec", 0.0, minimum=0.0),
            stake=_q_float(query, "stake", 100.0, minimum=0.0),
        )
        if d is None:
            raise NotFound("无该市场快照：%s" % match_id)
        return d

    def h_microstructure(self, query: Mapping[str, List[str]],
                         body: Mapping[str, Any],
                         match_id: str) -> Dict[str, Any]:
        d = self.svc.get_microstructure(
            match_id, market=_q1(query, "market", "1X2") or "1X2")
        if d is None:
            raise NotFound("无该市场快照：%s" % match_id)
        return d

    def h_portfolio(self, query: Mapping[str, List[str]],
                    body: Mapping[str, Any], *_a: str) -> Dict[str, Any]:
        mid = body.get("match_id")
        if not isinstance(mid, str) or not mid:
            raise BadRequest("请求体缺少 match_id")
        market = body.get("market")
        d = self.svc.get_portfolio(
            mid,
            market=(market if isinstance(market, str) and market else "1X2"),
            lam=_body_float(body, "lam", 0.25, minimum=0.0, maximum=1.0),
            rho=_body_float(body, "rho", 0.0, minimum=-0.99, maximum=0.99),
            max_total_exposure=_body_float(
                body, "max_total_exposure", 0.25, minimum=1e-9, maximum=1.0),
        )
        if d is None:
            raise NotFound("无法为该赛事生成仓位建议：%s" % mid)
        return d

    def h_calibration(self, query: Mapping[str, List[str]],
                      body: Mapping[str, Any], *_a: str) -> Dict[str, Any]:
        n_bins = _q_int(query, "n_bins", 10, minimum=1, maximum=100)
        return self.svc.get_calibration(n_bins=n_bins)

    def h_collect(self, query: Mapping[str, List[str]],
                  body: Mapping[str, Any], *_a: str) -> Dict[str, Any]:
        urls = body.get("urls")
        if not isinstance(urls, (list, tuple)) or not urls:
            raise BadRequest("请求体需要非空 urls 数组")
        clean = [u for u in urls if isinstance(u, str) and u.strip()]
        if not clean:
            raise BadRequest("urls 中没有有效字符串项")
        if len(clean) > 50:
            raise BadRequest("单次最多 50 个 URL")
        return self.svc.start_collect(clean, registry=self.registry)

    def h_task(self, query: Mapping[str, List[str]],
               body: Mapping[str, Any], task_id: str) -> Dict[str, Any]:
        t = self.registry.get(task_id)
        if t is None:
            raise NotFound("任务不存在：%s" % task_id)
        return t.as_dict()


# --------------------------------------------------------------------------- #
# HTTP 适配壳
# --------------------------------------------------------------------------- #

def create_app(
    snapshot_root: str,
    corpus_root: Optional[str] = None,
    registry: Optional[TaskRegistry] = None,
) -> ApiApp:
    """构造 API 应用（供 WSGI/测试使用）。"""
    svc = ValuationService(snapshot_root=snapshot_root,
                           corpus_root=corpus_root)
    return ApiApp(svc, registry=registry)


class _Handler(BaseHTTPRequestHandler):
    """把 http.server 的请求适配到 ApiApp。"""

    server_version = "ai_football/1.0"
    app: ApiApp  # 由 make_server 注入

    def _send(self, status: int, payload: Mapping[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False,
                         allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query, keep_blank_values=False)
            body: Mapping[str, Any] = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY_BYTES:
                    self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                               {"error": "请求体过大"})
                    return
                raw = self.rfile.read(length) if length > 0 else b""
                if raw:
                    try:
                        parsed_body = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        self._send(HTTPStatus.BAD_REQUEST,
                                   {"error": "请求体不是合法 JSON"})
                        return
                    if not isinstance(parsed_body, dict):
                        self._send(HTTPStatus.BAD_REQUEST,
                                   {"error": "请求体顶层应为对象"})
                        return
                    body = parsed_body
            status, payload = self.app.dispatch(method, parsed.path, query, body)
            self._send(status, payload)
        except Exception as exc:  # pragma: no cover - 最后兜底
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR,
                       {"error": "服务器内部错误", "detail": str(exc)[:200]})

    def do_GET(self) -> None:      # noqa: N802 - http.server 约定
        self._handle("GET")

    def do_POST(self) -> None:     # noqa: N802
        self._handle("POST")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(HTTPStatus.NO_CONTENT, {})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # 参数名必须与基类一致（format），否则类型检查器报 override 不兼容。
        # 默认实现会写 stderr；此处收敛，避免污染日志。
        if os.environ.get("API_ACCESS_LOG", "").lower() in ("1", "true"):
            super().log_message(format, *args)


def make_server(app: ApiApp, host: str, port: int) -> ThreadingHTTPServer:
    """构造 HTTP 服务器（应用与服务器解耦，便于测试注入随机端口）。"""
    handler = type("_BoundHandler", (_Handler,), {"app": app})
    return ThreadingHTTPServer((host, port), handler)


def _env_int(name: str, default: int,
             minimum: Optional[int] = None) -> int:
    """读取整型环境变量；非法值回退默认（启动配置不应导致崩溃）。"""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        val = int(raw.strip())
    except (TypeError, ValueError, OverflowError):
        print("警告：环境变量 %s=%r 不是整数，使用默认值 %d" % (name, raw, default))
        return default
    if minimum is not None and val < minimum:
        print("警告：环境变量 %s=%d 小于最小值 %d，使用最小值"
              % (name, val, minimum))
        return minimum
    return val


def run_server(host: str = "0.0.0.0", port: int = 8000,
               snapshot_root: str = "/app/output/snapshots",
               corpus_root: Optional[str] = None) -> None:
    """启动 API 服务（阻塞）。"""
    app = create_app(snapshot_root, corpus_root)
    srv = make_server(app, host, port)
    print("ai_football API 监听 http://%s:%d" % (host, port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":  # pragma: no cover
    run_server(
        host=os.environ.get("API_HOST", "0.0.0.0"),
        port=_env_int("API_PORT", 8000, minimum=1),
        snapshot_root=os.environ.get("SNAPSHOT_ROOT", "/app/output/snapshots"),
        corpus_root=os.environ.get("CORPUS_ROOT") or None,
    )
