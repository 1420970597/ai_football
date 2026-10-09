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
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urlparse

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


def _as_float(value: Any, default: float = 0.0) -> float:
    """容错浮点转换（看板字段可能缺失/为 None/为字符串）。

    看板是展示层：一个字段缺失不应让整页 500，宁可降级为默认值。
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    """容错整数转换（同上）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_int_or_none(value: Any) -> Optional[int]:
    """容错整数转换；不可转换返回 None（用于“要么给出合法值、要么不给”）。

    区别于 `_as_int`：这里 None 有语义（如比分拿不到就不要写进响应），
    而不能默默变成 0 —— 那会显示成“0:0”，与“没有比分”完全不同。
    """
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


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


def _body_int(body: Mapping[str, Any], name: str, default: int,
              minimum: Optional[int] = None,
              maximum: Optional[int] = None) -> int:
    """从请求体提取整数字段（可选），带类型与区间校验。

    与 `_body_float` 同一约定：请求体是外部输入，转换失败必须是 400。
    POST /decisions/job 需要 limit/workers，因此单独提供整数版本。
    """
    if name not in body or body[name] is None:
        return default
    raw = body[name]
    if isinstance(raw, bool):
        raise BadRequest("%s 不能是布尔值" % name)
    if isinstance(raw, int):
        val = raw
    elif isinstance(raw, float):
        # `int(inf)` 会抛 OverflowError、`int(nan)` 会抛 ValueError；
        # 虽然下面的有限性校验会先拦住，但显式 try 包裹更稳健
        # （自定义 float 子类可绕过短路判断），同时满足静态规则。
        try:
            if not math.isfinite(raw) or raw != int(raw):
                raise BadRequest("字段 %s 必须是整数: %r" % (name, raw))
            val = int(raw)
        except (OverflowError, ValueError) as exc:
            raise BadRequest("字段 %s 必须是整数: %r" % (name, raw)) from exc
    elif isinstance(raw, str):
        try:
            val = int(raw.strip())
        except (TypeError, ValueError) as exc:
            raise BadRequest("字段 %s 不是整数: %r" % (name, raw)) from exc
    else:
        raise BadRequest("字段 %s 类型不支持: %s" % (name, type(raw).__name__))
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
                 analysis: Optional[Any] = None) -> None:
        self.svc = service
        #: 分析层（实时推送 + 决策引擎）。未注入时按需惰性构造，
        #: 避免每次测试构造 API 都去连上游。
        self._analysis = analysis

    @property
    def analysis(self) -> Any:
        """惰性构造分析服务（含后台实时推送）。"""
        if self._analysis is None:
            from service.analysis import build_analysis_service

            self._analysis = build_analysis_service(self.svc, realtime=None)
        return self._analysis

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
            # 决策与实时（T5/T7）：决策列表直接供控制台渲染
            ("GET", "/decisions", self.h_decisions),
            ("POST", "/decisions/job", self.h_decisions_job),
            ("GET", "/decisions/job/<id>", self.h_decisions_job_status),
            ("GET", "/decisions/<id>", self.h_decision_detail),
            ("GET", "/trend/<id>", self.h_trend),
            ("GET", "/realtime", self.h_realtime),
            # 分析层健康状况：含**进行中覆盖的判据**（source/count/error）。
            # 用户要能自己回答「为什么系统显示的进行中比 leyu 少」：
            # 早期 `AnalysisService.health()` 已算好这些数据，
            # 但**没有任何端点暴露它**，调用方无从查看。
            ("GET", "/analysis", self.h_analysis),
            ("GET", "/board", self.h_board),
            ("GET", "/workbench", self.h_workbench),
            ("GET", "/workbench/<id>", self.h_workbench_detail),
            ("GET", "/llm", self.h_llm),
            ("GET", "/settings", self.h_settings),
            ("POST", "/settings", self.h_settings_save),
            ("GET", "/ledger/stats", self.h_ledger_stats),
            # 历史战绩：分日期/联赛/盘口 的分组统计 + 逐条明细（含实际比分）。
            # 与 /ledger/stats 互补：后者是“一句话结论”，本端点是
            # “申诉材料”，用回答“为什么不行、哪一类不行”。
            ("GET", "/ledger/history", self.h_ledger_history),
            ("GET", "/ledger/entries", self.h_ledger_entries),
            ("POST", "/ledger/settle", self.h_ledger_settle),
            ("GET", "/consistency/<id>", self.h_consistency),
            ("GET", "/fair/<id>", self.h_fair),
            ("GET", "/edge/<id>", self.h_edge),
            ("GET", "/microstructure/<id>", self.h_microstructure),
            ("POST", "/portfolio", self.h_portfolio),
            ("GET", "/calibration", self.h_calibration),
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
            from service.runtime_settings import VersionConflict
            if isinstance(exc, VersionConflict):
                return HTTPStatus.CONFLICT, {"error": str(exc)}
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
        """赛事列表。

        支持 `?refresh=1`：先确保当前数据源有新鲜数据再返回。
        加上 `&full=1` 则进行**全量采集**（保证赛事完整，约 40 秒）。
        这样控制台打开时看到的就是当前数据源的真实数据，
        而不会因为存储里只有旧源而显示过期内容。
        默认不自动采集（避免每次翻页都打上游）。
        """
        refreshed: Optional[Dict[str, Any]] = None
        if _q1(query, "refresh") not in (None, "", "0", "false"):
            full = _q1(query, "full") not in (None, "", "0", "false")
            limit = _q_int(query, "limit", 0) or None
            refreshed = self.svc.ensure_fresh(
                max_matches=None if full else (limit or 40), full=full)
        items = self.svc.list_matches(
            date=_q1(query, "date"),
            league=_q1(query, "league"),
            query=_q1(query, "q"),
        )
        out: Dict[str, Any] = {
            "count": len(items),
            "matches": items,
            "data_source": self.svc.source.display_source,
            "source_id": self.svc.source.name,
        }
        if refreshed is not None:
            out["refresh"] = refreshed
        return out

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
                "这是**已知数据缺口**，需重新采集而非重试（当前数据源：%s）。"
                "体彩源的落盘 JSON 仅存 HAD/HHAD 赔率；"
                "乐鱼源覆盖 HAD/AH(<line>)/OU(<line>) 及上半场玩法，"
                "但不含 TTG/CRS/HAFU（见 docs/architecture/leyu-api-protocol.md）。"
                % (market, ", ".join(m["market"] for m in known["markets"]),
                   self.svc.source.name)
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

    # -- 决策与实时（T5/T7） ------------------------------------------------

    def h_workbench(self, query: Mapping[str, List[str]],
                    body: Mapping[str, Any], *_a: str) -> Dict[str, Any]:
        kind = _q1(query, "type", "real")
        if kind not in ("real", "virtual", "unknown", "all"):
            raise BadRequest("type 必须是 real/virtual/unknown/all")
        result = self.analysis.live_expert.results(kind)
        rt = self.analysis.realtime
        # A one-second UI poll must not scan/lock the complete persisted book.
        realtime = ({"running": rt.running, **rt.stats.as_dict()} if rt is not None
                    else {"running": False, "connected": 0})
        rows = []
        for row in result.pop("decisions"):
            public = self._public_live_row(row, bool(realtime.get("connected")))
            public["llm_review"] = self.analysis.live_review.public(row)
            public.pop("evaluations", None)
            public.pop("candidates", None)
            public.pop("events", None)
            rows.append(public)
        rows.sort(key=lambda r: (r["stale"], r.get("league", ""), r["match_id"]))
        result.update(
            matches=rows,
            realtime=realtime,
            coverage=self._workbench_coverage(kind, len(rows), rt),
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
        return result

    def _workbench_coverage(self, kind: str, displayed: int,
                            realtime: Optional[Any]) -> Dict[str, Any]:
        """Expose the four counts needed to audit the workbench list.

        ``live_expert.results`` only contains matches that have completed at
        least one local computation.  Treating that number as the source
        total made a healthy subscription look like a one-match feed while
        the rest of the current schedule was still entering the pipeline.
        Keep source, subscription, analysis and display counts separate.
        """
        try:
            coverage = self.analysis.live_coverage(kind)
        except (AttributeError, TypeError):
            coverage = {}
        if not isinstance(coverage, dict):
            coverage = dict(coverage) if isinstance(coverage, Mapping) else {}
        try:
            current_ids = self.analysis.live_match_ids_by_type(kind, refresh=False)
        except (AttributeError, TypeError):
            current_ids = None
        try:
            subscribed_ids = set(realtime.subscribed()) if realtime is not None else set()
        except (AttributeError, TypeError):
            subscribed_ids = set()
        try:
            quote_matches = realtime.live.n_matches() if realtime is not None else None
        except (AttributeError, TypeError):
            quote_matches = None
        coverage.update(
            subscribed=(len(current_ids & subscribed_ids)
                        if current_ids is not None else None),
            subscribed_total=len(subscribed_ids),
            quotes=quote_matches,
            analyzed=displayed,
            displayed=displayed,
        )
        return coverage

    @staticmethod
    def _public_live_row(row: Mapping[str, Any], connected: bool) -> Dict[str, Any]:
        import time
        public = dict(row)
        age = max(0, time.time() - _as_float(row.get("published_at_ms")) / 1000)
        quote_age = max(0, time.time() - _as_float(row.get("quote_time_ms")) / 1000)
        public.update(result_age_s=round(age, 1), quote_age_s=round(quote_age, 1),
                      stale=not connected or age > 15 or quote_age > 15)
        if public["stale"]:
            public["picks"] = []
            public["has_buy"] = False
            public["decision"] = "observe"
        return public

    def h_workbench_detail(self, query: Mapping[str, List[str]],
                           body: Mapping[str, Any], match_id: str) -> Dict[str, Any]:
        row = self.analysis.live_expert.detail(match_id)
        if row is None:
            raise NotFound("该场尚未收到实时算法结果")
        rt = self.analysis.realtime
        connected = bool(rt is not None and rt.stats.connected)
        public = self._public_live_row(row, connected)
        public["llm_review"] = self.analysis.live_review.public(row)
        return public

    def h_settings(self, query: Mapping[str, List[str]],
                   body: Mapping[str, Any], *_a: str) -> Dict[str, Any]:
        return {**self.analysis.runtime_settings.public(), "llm_review": self.analysis.live_review.health()}

    def h_settings_save(self, query: Mapping[str, List[str]],
                        body: Mapping[str, Any], *_a: str) -> Dict[str, Any]:
        patch = body.get("settings")
        if not isinstance(patch, Mapping):
            raise BadRequest("settings 必须是对象")
        return self.analysis.update_settings(patch, body.get("version"))

    def h_decisions(self, query: Mapping[str, List[str]],
                    body: Mapping[str, Any], *_a: str) -> Dict[str, Any]:
        """决策列表（**默认读后台定时产生的结果，不触发 LLM**）。

        设计要点（用户要求）：决策由**后台定时异步**执行，
        页面刷新不应触发一轮 LLM。因此本端点默认读缓存，毫秒级返回。

        参数：
            cached=1（默认）  读后台定时结果，不跑 LLM
            cached=0          同步重算（**慢**，可能 504，仅供调试）
            async=1           起一个后台任务并返回 job_id（手动强制刷新）
            live=1            只看进行中的赛事（仅用于同步/异步重算）
            league=xxx        限定联赛
            max_age=600       超过该秒数则标 `stale=true`

        关于 `stale`（用户报“加载不出来比赛场次”的真实故障）：
        早期实现是“超龄就返回 None”，于是页面只能显示“等待后台首轮决策”。
        但一轮全量决策实测要跑 **50+ 分钟**，其至比 max_age 还长 ——
        结果刚产出就已“过期”，页面**永远空着**，而磁盘上明明有 2436 场。

        正确语义：**旧数据也要给，只是如实标注它旧**。
        隐藏它等于把“慢”伪装成“无”，用户反而更看不出问题在哪。
        """
        mode = (_q1(query, "cached") or "1").strip().lower()
        if mode not in ("0", "false", "no"):
            max_age = _q_int(query, "max_age", 600)
            # 先不带时效限制取回（以便区分“没有结果”与“结果太旧”）
            res = self.analysis.latest_result(max_age_s=None)
            if res is None:
                # 真的尚未产出过任何结果（如刚启动、首轮还在跑）
                return {
                    "count": 0, "summary": {}, "decisions": [],
                    "pending": True,
                    "cycle": getattr(self.analysis, "cycle_stats", {}),
                    "hint": "后台决策尚无任何产出（首轮可能仍在运行）；"
                            "可稍后刷新，或调用 POST /decisions/job 手动触发一轮。",
                }
            age_s = self.analysis.latest_age_s()
            stale = bool(max_age and age_s is not None and age_s > max_age)
            out = dict(res)
            date = _q1(query, "date")
            league = _q1(query, "league")
            keyword = (_q1(query, "q") or "").strip().casefold()
            if date or league or keyword:
                # Older persisted decisions lack kickoff dates. Resolve them
                # from local snapshots only when a date filter is requested.
                dated_ids = ({str(m["match_id"])
                              for m in self.svc.list_matches(date=date)}
                             if date else None)
                rows = [d for d in (res.get("decisions") or [])
                        if (dated_ids is None or str(d.get("match_id")) in dated_ids)
                        and (not league or d.get("league") == league)
                        and (not keyword or keyword in " ".join(
                            str(d.get(k) or "") for k in
                            ("match_id", "league", "home", "away")).casefold())]
                out["decisions"] = rows
                out["count"] = len(rows)
                out["summary"] = {
                    **(res.get("summary") or {}),
                    "n": len(rows),
                    "buy": sum(bool(d.get("picks")) for d in rows),
                    "watch": sum(d.get("decision") == "watch" for d in rows),
                    "avoid": sum(d.get("decision") == "avoid" for d in rows),
                    "no_llm": sum(d.get("decision") == "no_llm" for d in rows),
                    "n_picks": sum(len(d.get("picks") or []) for d in rows),
                }
            out["cached"] = True
            # 如实的时效标注：前端据此提示“数据较旧，后台正在重算”
            out["stale"] = stale
            out["age_s"] = (round(age_s, 1) if age_s is not None else None)
            out["max_age_s"] = max_age
            out["cycle"] = getattr(self.analysis, "cycle_stats", {})
            return out

        limit = _q_int(query, "limit", 8) or 8
        only_live = _q1(query, "live") not in (None, "", "0", "false")
        limit = max(1, min(limit, 50))

        # 异步模式：LLM 决策耗时数十秒，同步容易 504
        if _q1(query, "async") not in (None, "", "0", "false"):
            job_id = self.analysis.start_job(
                limit=limit, only_live=only_live,
                league=_q1(query, "league"),
                max_workers=_q_int(query, "workers", 4),
            )
            return {"job_id": job_id, "state": "running",
                    "poll": API_PREFIX + "/decisions/job/" + job_id}

        return self.analysis.decide_list(
            limit=limit, only_live=only_live,
            league=_q1(query, "league"),
            max_workers=_q_int(query, "workers", 4),
        )

    def h_decisions_job(self, query: Mapping[str, List[str]],
                        body: Mapping[str, Any], *_a: str) -> Dict[str, Any]:
        """创建决策任务（POST 形式，等价于 `?async=1`）。"""
        limit = _body_int(body, "limit", 8)
        job_id = self.analysis.start_job(
            limit=max(1, min(limit, 50)),
            only_live=bool(body.get("live")),
            league=body.get("league") or None,
            max_workers=_body_int(body, "workers", 4),
        )
        return {"job_id": job_id, "state": "running",
                "poll": API_PREFIX + "/decisions/job/" + job_id}

    def h_decisions_job_status(self, query: Mapping[str, List[str]],
                               body: Mapping[str, Any], job_id: str) -> Dict[str, Any]:
        """轮询决策任务状态；完成时携带 result。"""
        st = self.analysis.job_status(job_id)
        if st is None:
            raise NotFound("任务不存在：%s" % job_id)
        return st

    def h_decision_detail(self, query: Mapping[str, List[str]],
                          body: Mapping[str, Any], match_id: str) -> Dict[str, Any]:
        """单场决策明细（含每个候选结果的概率拆解与 LLM 理由）。"""
        market = _q1(query, "market")
        d = self.analysis.analyze(
            match_id, market=market,
            force=_q1(query, "force") not in (None, "", "0", "false"))
        if d is None:
            raise NotFound("无该赛事或该场无可分析市场：%s" % match_id)
        return d.as_dict()

    def h_trend(self, query: Mapping[str, List[str]],
                body: Mapping[str, Any], match_id: str) -> Dict[str, Any]:
        """盘口走势（来自实时推送的真实赔率变动）。"""
        return self.analysis.realtime.trend(match_id) if self.analysis.realtime \
            else {"mid": match_id, "n_markets": 0, "markets": [],
                    "note": "实时推送未启用"}

    def h_realtime(self, query: Mapping[str, List[str]],
                   body: Mapping[str, Any], *_a: str) -> Dict[str, Any]:
        """实时推送健康与统计（判断采集频率是否正常）。"""
        if self.analysis.realtime is None:
            return {"running": False, "note": "实时推送未启用"}
        return self.analysis.realtime.health()

    def h_analysis(self, query: Mapping[str, List[str]],
                   body: Mapping[str, Any], *_a: str) -> Dict[str, Any]:
        """分析层健康状况：含 **进行中覆盖的判据**（供对账 leyu）。

        为何单独开这个端点（用户报「leyu 67 场 vs 系统 34 场」时最需要它）：
        `AnalysisService.health()` 里已经算好了 `live.{source,count,error}`，
        但早期**没有任何端点暴露它**，用户/运维无从查看，只能猜。
        现在可直接：

            curl /api/v1/analysis | jq .live
            # {"source":"push","count":41,"age_s":2.1,"error":""}

        `source` 的取值含义：
          * `schedule` —— 走 REST 赛程（最准）
          * `push`     —— REST 不可用，用推送见过的场次回退（会话过期的兜底）
          * `none`     —— 两条路都不可用（此时页面覆盖必然不全）
        """
        return self.analysis.health()

    def h_llm(self, query: Mapping[str, List[str]],
              body: Mapping[str, Any], *_a: str) -> Dict[str, Any]:
        """LLM 配置与调用统计（密钥一律脱敏）。"""
        return self.analysis.llm_health()

    # -- 逐场逐盘口实时看板（用户要求：能看到每场每个盘口的实时信息） --------

    def h_board(self, query: Mapping[str, List[str]],
                body: Mapping[str, Any], *_a: str) -> Dict[str, Any]:
        """逐场逐盘口的实时看板（一次请求给齐渲染所需数据）。

        为何不拆成多个端点让前端拼：前端要同时展示「比赛 + 每个盘口的
        最新赔率 + 每个盘口的走势 + 门控结论」。分多次拉取会出现
        「赔率是这一刻、门控结论是上一刻」的**时间戳裂**，用户看到的
        信息互相矛盾。这里在服务端对齐到同一时刻。

        数据来源（按优先级）：
          1. 最近一轮决策的 `computations` —— 信息最全（含 line / 去水 /
             edge / 门控 / 走势）。**读缓存，不触发 LLM**。
          2. 该场没有决策结果时，回退用快照库的最新赔率（只有赔率，
             没有门控结论）。前端必须如实标注「未决策」，
             而不是显示一个假的「通过」。

        Query:
            live=1       只看进行中
            league=xx    限定联赛
            limit=N      最多几场（默认 0 = 全部）
            markets=0    不带逐盘口明细（只要列表，减小体积）
        """
        from core.market_labels import describe_market

        only_live = (_q1(query, "live") or "") not in ("", "0", "false")
        league = (_q1(query, "league") or "").strip() or None
        limit = _q_int(query, "limit", 0, minimum=0, maximum=2000)
        with_markets = (_q1(query, "markets") or "1") not in (
            "0", "false", "no")

        live_ids = self.analysis.live_match_ids()
        res = self.analysis.latest_result(max_age_s=None) or {}
        dec_by_id: Dict[str, Mapping[str, Any]] = {}
        for d in (res.get("decisions") or []):
            if isinstance(d, Mapping):
                dec_by_id[str(d.get("match_id"))] = d
        rt = self.analysis.realtime

        # **能否判定“进行中”**（用户问题 1：场次要与乐鱼一致）。
        #
        # 为何需要这个标志：早期在 `live_ids is None`（赛程与推送都不可用）
        # 时回退为 `state == "active"`，而那个 state 只是**快照时效**派生
        # 的（active/stale/delisted），与“比赛是否在踢”无关 ——
        # 实测页面因此声称 **2436 场进行中**，而乐鱼只有 67 场，
        # 差异高达 36 倍，属于**误导性展示**。
        #
        # 正确做法：拿不到权威判据时就不声称“进行中”，
        # 而是把这件事**如实告知**（`live_known=False` + 原因），
        # 让用户知道“这个数字不可信、需要修凭据”，而不是被骗。
        live_known = live_ids is not None

        rows: List[Dict[str, Any]] = []
        for m in self._board_candidates(league=league, live_ids=live_ids,
                                        only_live=only_live, rt=rt):
            mid = str(m.get("match_id") or "")
            state = str(m.get("state") or "")
            if state == "delisted":
                continue
            # 只有**权威判据**（赛程 ms==1 或推送见过）才算进行中；
            # 拿不到判据时一律当作“未知”，不得拿快照时效冒充。
            is_live = bool(live_known and mid in (live_ids or ()))
            # ⚠️ 只在**能判定**时才按“进行中”过滤。
            # 若判据未知（`live_known=False`）还硬过滤，就会重现
            # “看板空白”的故障（前端默认勾选「只看进行中」）。
            # 此时宁可全部展示并标注 `live_known=False`，让用户
            # 知道“这个数字不可信”，而不是给他一个空页。
            if only_live and live_known and not is_live:
                continue
            dec = dec_by_id.get(mid) or {}
            row: Dict[str, Any] = {
                "match_id": mid,
                "league": str(m.get("league") or ""),
                "home": str(m.get("home") or ""),
                "away": str(m.get("away") or ""),
                "date": str(m.get("date") or ""),
                "time": str(m.get("time") or ""),
                "state": state,
                "is_live": is_live,
                "score": None,
                "half_score": None,
                "clock": "",
                "n_markets": len(m.get("markets") or []),
                # 决策结论；未决策时保持中性值，前端据此标注
                "decided": bool(dec),
                "decision": str(dec.get("decision") or ""),
                "has_buy": bool(dec.get("has_buy")),
                "best_label": str(dec.get("best_label") or ""),
                "gated_in": _as_int(dec.get("gated_in")),
                "gated_out": _as_int(dec.get("gated_out")),
                "reject_reasons": list(dec.get("reject_reasons") or []),
                "llm_used": bool(dec.get("llm_used")),
                "llm_confidence": _as_float(dec.get("llm_confidence")),
                "llm_reason": str(dec.get("llm_reason") or ""),
                "picks": list(dec.get("picks") or []),
                "decided_at": str(dec.get("computed_at") or ""),
            }
            self._fill_live_state(row, mid, rt)
            # Even list-only callers must not see a buy at an obsolete price.
            row["markets"] = self._board_markets(m, dec, describe_market)
            markets = row["markets"]
            valid_markets = {mk["market"] for mk in markets
                             if mk["decided"] and not mk["decision_stale"]}
            row["decision_stale"] = any(mk["decision_stale"] for mk in markets)
            row["picks"] = [p for p in row["picks"]
                            if p.get("market") in valid_markets]
            row["has_buy"] = bool(row["picks"])
            row["best_label"] = (str(max(row["picks"],
                key=lambda p: _as_float(p.get("edge"))).get("pick_label") or "")
                if row["picks"] else "")
            row["gated_in"] = sum(bool(mk["gate_passed"]) for mk in markets)
            if not with_markets:
                del row["markets"]
            rows.append(row)

        # 排序：有买入建议 → 进行中 → 其余；同级按开赛时间
        rows.sort(key=lambda r: (
            0 if r["has_buy"] else (1 if r["is_live"] else 2),
            str(r.get("date") or ""), r["match_id"]))
        if limit:
            rows = rows[:limit]
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "count": len(rows),
            "live": sum(1 for r in rows if r["is_live"]),
            "buy": sum(1 for r in rows if r["has_buy"]),
            "decided": sum(1 for r in rows if r["decided"]),
            # 进行中判据是否可信（用户问题 1 的核心）——
            # False 时前端必须提示“该数字不可信”，而不是直接展示。
            "live_known": live_known,
            "live_source": self.analysis._live_source,
            "live_error": self.analysis._live_error,
            "summary": res.get("summary") or {},
            "trigger": self.analysis.scheduler_health(),
            "cycle": getattr(self.analysis, "cycle_stats", {}),
            "realtime": (rt.health() if rt is not None
                         else {"running": False}),
            "matches": rows,
        }

    # -- 看板候选：读**本地实时表**，不扫快照库（用户要求 3） ---------------

    def _board_candidates(self, league: Optional[str],
                         live_ids: Optional[set], only_live: bool,
                         rt: Any) -> List[Dict[str, Any]]:
        """构造看板候选场次：**只读本地实时表 + 目录名索引**。

        ## 为何不用 `svc.list_matches()`（用户报告问题 3）

        `list_matches()` 会调 `_all_snapshots()` 读 6.5 万个快照文件
        （实测 **14s**，冷启动叠加到 45s）。而实时看板真正需要的是：

          * 有哪些场、队名、联赛 → **目录名里就有**（扫 0.09s）；
          * 现在什么赔率 → **推送已经推过了**（`LiveBook` 内存表）。

        两者都是毫秒级，因此看板不再需要碰磁盘上的历史快照。

        Args:
            league: 限定联赛。
            live_ids: `live_match_ids()` 的结果（可能为 None）。
            only_live: 只看进行中。
            rt: `RealtimeHub`（可能为 None）。

        Returns:
            与 `list_matches()` 兼容的字典列表（额外带 `_live_odds`）。
        """
        from collector.leyu_normalizer import snapshots_from_live

        # 没有实时推送时（离线回放/测试）根本没有“本地实时表”
        # 可用，此时只能读快照库 —— 这是唯一可行路径，不是性能回退。
        if rt is None:
            return list(self.svc.list_matches(league=league))

        names = {r["match_id"]: r for r in self.svc.match_index()}

        # 取实时表（旧版 Hub 没有 `live` 属性 → 视为无行情，走回退路径）。
        # 用 getattr 而不是 `rt.live`：既兼容旧 Hub，也让类型检查明确
        # “这个属性可能存在也可能不存在”。
        book: Any = getattr(rt, "live", None)

        # 1) 有实时行情的场次（看板主体）；顺序按 mid 保证输出可复现
        live_mids: List[str] = []
        if book is not None:
            try:
                live_mids = list(book.live_mids())
            except (AttributeError, TypeError):
                book = None     # 旧版/异常实现 → 降级为快照库

        # 没有任何实时行情时退回快照库。
        #
        # 为何不再要求 `not only_live`（本项目真实故障）：前端默认勾选
        # 「只看进行中」，于是 `only_live=True`；而会话不可用时 `live_mids`
        # 为空 → 早期实现直接越过回退、返回空列表 → 看板永远显示
        # “暂无进行中的盘口”，而磁盘上明明有几千场。现在统一回退，
        # 由调用方按 `is_live` 过滤（它本来就会做这一步）。
        if not live_mids:
            return list(self.svc.list_matches(league=league))
        # 排除“收集到了 mid 但实时表已降级”的矛盾状态，让类型收窄为非 None。
        if book is None:
            return list(self.svc.list_matches(league=league))

        out: List[Dict[str, Any]] = []
        seen: set = set()
        src_name = self.svc.source.display_source
        for mid in live_mids:
            if mid in seen:
                continue
            seen.add(mid)
            nm = names.get(mid) or {}
            lg = str(nm.get("league") or "")
            if league and lg != league:
                continue
            snaps = snapshots_from_live(book.book(mid), mid,
                                        source=src_name)
            odds_by: Dict[str, List[float]] = {}
            outs_by: Dict[str, List[str]] = {}
            lines_by: Dict[str, str] = {}
            for s in snaps:
                odds_by[s.market] = [round(o, 4) for o in s.odds]
                outs_by[s.market] = list(s.outcomes)
                lines_by[s.market] = str((s.metadata or {}).get("leyu_hv") or "")
            out.append({
                "match_id": mid,
                "league": lg,
                "home": str(nm.get("home") or ""),
                "away": str(nm.get("away") or ""),
                "date": "",
                "time": "",
                # 有实时行情 → 就是在滚球（state 供前端样式用）
                "state": "active",
                "markets": sorted(odds_by),
                "latest_odds": odds_by,
                "latest_outcomes": outs_by,
                "latest_lines": lines_by,
                "_quote_source": "live",
            })

        # 2) 兜底：无实时行情的场次（未开赛/未被订阅）。
        #    只给出名称，**不读快照**，避免把看板拖慢；赔率留空由前端标「无行情」。
        if not only_live:
            for mid, nm in names.items():
                if mid in seen:
                    continue
                lg = str(nm.get("league") or "")
                if league and lg != league:
                    continue
                is_live = (str(mid) in live_ids) if live_ids is not None \
                    else False
                out.append({
                    "match_id": mid,
                    "league": lg,
                    "home": str(nm.get("home") or ""),
                    "away": str(nm.get("away") or ""),
                    "date": "",
                    "time": "",
                    "state": "active" if is_live else "stale",
                    "markets": [],
                    "latest_odds": {},
                    "latest_outcomes": {},
                })
        return out

    @staticmethod
    def _fill_live_state(row: Dict[str, Any], mid: str, rt: Any) -> None:
        """填比分 / 半场比分 / 比赛时钟（来自实时推送，缺失则留空）。

        容错契约：推送载荷不可信（可能是字符串/None/异常值），
        而本方法在渲染 `/board` 的热路径上 —— 一个脏字段绝不能
        让整页 500。取不到合法值时**不写字段**（而不是写 0:0），
        因为“没有比分”与“0:0”语义完全不同。
        """
        if rt is None:
            return
        try:
            sc = rt.score(mid)
        except Exception:  # noqa: BLE001 - 单个字段缺失不应让整页 500
            sc = None
        if sc and len(sc) >= 2:
            hi, ai = _as_int_or_none(sc[0]), _as_int_or_none(sc[1])
            if hi is not None and ai is not None:
                row["score"] = [hi, ai]
        try:
            st = rt.status(mid)
        except Exception:  # noqa: BLE001
            st = {}
        if not isinstance(st, Mapping):
            st = {}
        row["clock"] = str(st.get("clock") or st.get("minute") or "")
        ht = st.get("half_score")
        if isinstance(ht, (list, tuple)) and len(ht) >= 2:
            h0, h1 = _as_int_or_none(ht[0]), _as_int_or_none(ht[1])
            if h0 is not None and h1 is not None:
                row["half_score"] = [h0, h1]

    @staticmethod
    def _board_markets(m: Mapping[str, Any], dec: Mapping[str, Any],
                       describe: Any) -> List[Dict[str, Any]]:
        """逐盘口明细。

        优先用决策里的 `computations`：它含**原始线值**、去水概率、edge、
        门控结论与走势 —— 这些是判断「为什么没建议买入」的关键。
        没有决策结果时才回退到快照库的最新赔率（并标记 `decided=False`）。

        用户要求：盘口信息与买入建议一律以**中文**展示，且与乐鱼一致
        （如「曼联上半场-1」「上半场进球数>1/1.5」）。因此每个结果都
        补一个 `outcome_labels`（与 `outcomes` 同序），前端直接渲染即可，
        无需在前端重复实现一套翻译（那会导致两处措辞逐渐漂移）。
        """
        from core.market_labels import format_market

        home = str(m.get("home") or "")
        away = str(m.get("away") or "")

        def _labels(market: object, line: object,
                    outcomes: Sequence[Any]) -> List[str]:
            """每个结果的**中文选项名**（乐鱼风格，含队名）。"""
            return [format_market(market, oc, line, home=home, away=away)
                    for oc in outcomes]

        comps = dec.get("computations") or []
        out: List[Dict[str, Any]] = []
        if comps:
            for c in comps:
                if not isinstance(c, Mapping):
                    continue
                outs = list(c.get("outcomes") or [])
                ln = str(c.get("line") or "")
                mk = str(c.get("market") or "")
                out.append({
                    "market": mk,
                    "label": str(c.get("market_label") or ""),
                    "line": ln,
                    "outcomes": outs,
                    # 中文选项名（与 outcomes 同序），供前端直接展示
                    "outcome_labels": _labels(mk, ln, outs),
                    "odds": list(c.get("odds") or []),
                    "p_fair": list(c.get("p_fair") or []),
                    "edges": list(c.get("edges") or []),
                    "kellys": list(c.get("kellys") or []),
                    "margin": _as_float(c.get("margin")),
                    "state": str(c.get("state") or ""),
                    "method": str(c.get("method") or ""),
                    "trend": str(c.get("trend") or ""),
                    "trend_pct": _as_float(c.get("trend_pct")),
                    "trend_n": _as_int(c.get("trend_n")),
                    "gate_passed": bool(c.get("gate_passed")),
                    "reject_reasons": list(c.get("reject_reasons") or []),
                    "best_edge": _as_float(c.get("best_edge")),
                    "best_outcome": str(c.get("best_outcome") or ""),
                    # 逐结果的入场门控结论（含门槛与余量），供人工复核
                    "gates": list(c.get("gates") or []),
                    "decided": True,
                })
        # Merge current markets, including newly quoted ones, then align each
        # computation with its actual input prices. Never relabel cached prices
        # as live, or apply an old edge/gate to a changed quote.
        odds_by = m.get("latest_odds") or {}
        oc_by = m.get("latest_outcomes") or {}
        existing = {r["market"] for r in out}
        for mk in sorted(odds_by):
            if mk in existing:
                continue
            outs = list(oc_by.get(mk) or [])
            line = str((m.get("latest_lines") or {}).get(mk) or "")
            out.append({
                "market": str(mk),
                "label": describe(mk),
                "line": line,
                "outcomes": outs,
                "outcome_labels": _labels(mk, line, outs),
                "odds": list(odds_by.get(mk) or []),
                "p_fair": [], "edges": [], "kellys": [],
                "margin": 0.0, "state": "", "method": "",
                "trend": "", "trend_pct": 0.0, "trend_n": 0,
                "gate_passed": False, "reject_reasons": [],
                "best_edge": 0.0, "best_outcome": "", "gates": [],
                "decided": False,
            })
        for row in out:
            mk = row["market"]
            decision_odds = list(row["odds"]) if row["decided"] else []
            decision_outcomes = list(row["outcomes"])
            current_odds = list(odds_by.get(mk) or [])
            current_outcomes = list(oc_by.get(mk) or [])
            available = bool(current_odds and len(current_odds) == len(current_outcomes))
            same = (available and row["outcomes"] == current_outcomes
                    and len(decision_odds) == len(current_odds)
                    and all(round(_as_float(a), 4) == round(_as_float(b), 4)
                            for a, b in zip(decision_odds, current_odds)))
            prices_by_outcome = dict(zip(decision_outcomes, decision_odds))
            row["decision_odds"] = ([prices_by_outcome.get(oc)
                                     for oc in current_outcomes]
                                    if available else decision_odds)
            row["decided_at"] = str(dec.get("computed_at") or "")
            row["quote_available"] = available
            row["quote_source"] = str(m.get("_quote_source") or "snapshot")
            row["decision_stale"] = bool(row["decided"] and not same)
            row["odds"] = current_odds if available else []
            if available:
                row["outcomes"] = current_outcomes
                row["outcome_labels"] = _labels(mk, row["line"], current_outcomes)
            if row["decision_stale"]:
                for field in ("p_fair", "edges", "kellys", "gates"):
                    row[field] = []
                row["gate_passed"] = False
                row["reject_reasons"] = ["quote_changed" if available else "quote_unavailable"]
                row["margin"] = None
                row["best_edge"] = None
                row["trend"] = ""
                row["trend_pct"] = 0
                row["trend_n"] = 0
        out.sort(key=lambda r: r["market"])
        return out

    # -- 决策台账与本地统计（回答“LLM 判定到底准不准”） ---------------------

    def h_ledger_stats(self, query: Mapping[str, List[str]],
                       body: Mapping[str, Any], *_a: str) -> Dict[str, Any]:
        """本地统计：命中率 / ROI / CLV。

        Query:
            all=1       同时统计**被门控拦截**的盘口（用于校准阈值）
            trigger     只看某个触发来源（`price_change` / `cycle`）
        """
        only_picks = (_q1(query, "all") or "") not in ("1", "true", "yes")
        trig = (_q1(query, "trigger") or "").strip() or None
        return self.analysis.ledger_stats(only_picks=only_picks,
                                          trigger=trig)

    def h_ledger_history(self, query: Mapping[str, List[str]],
                         body: Mapping[str, Any], *_a: str) -> Dict[str, Any]:
        """历史战绩：**决策 vs 实际结果**的分组统计 + 逐条明细。

        用户要求：「增加一个菜单展示历史决策与实际结果的统计」。

        为何不只返回总命中率：总量指标无法指导改进 ——
        总命中率 52% 可能掩盖「某联赛 20%」或「某盘口 80%」，
        后者才是可执行的信号。因此本端点同时给：

          * `overall` / `settled` —— 总量指标（含 CLV，比命中率更可信）
          * `by_date` / `by_league` / `by_market` —— 分组归因
          * `by_decision` / `by_trigger` —— 按裁定/触发源拆解
          * `timeline` —— 按日累计曲线（小样本下日命中率噪声大，
            看累计才能判断是真赚还是运气）
          * `entries` —— 逐条明细（含**实际终场比分**与输赢）

        Query:
            all=1      含被门控拦截的盘口（用于校准阈值）
            limit=N    明细条数（默认 300，最多 2000）
            days=N     只看最近 N 天（默认 0 = 不限）
        """
        only_picks = (_q1(query, "all") or "") not in ("1", "true", "yes")
        limit = _q_int(query, "limit", 300, minimum=1, maximum=2000)
        days = _q_int(query, "days", 0, minimum=0, maximum=3650)
        kind = _q1(query, "type")
        if kind not in (None, "all", "real", "virtual", "unknown"):
            raise BadRequest("type 必须是 real/virtual/unknown/all")
        cohort = _q1(query, "cohort", "all")
        if cohort not in ("all", "prospective", "recommendations", "legacy"):
            raise BadRequest("cohort 必须是 all/prospective/recommendations/legacy")
        return self.analysis.ledger_history(only_picks=only_picks,
                                            limit=limit, days=days, competition_type=kind,
                                            algorithm=_q1(query, "algorithm"), cohort=cohort)

    def h_ledger_entries(self, query: Mapping[str, List[str]],
                         body: Mapping[str, Any], *_a: str) -> Dict[str, Any]:
        """台账明细（可按状态/是否建议筛选），供页面逐条核对。

        Query:
            picks=1     只看买入建议（默认 1）
            status      只看某状态（`pending`/`won`/`lost`/`push`/`void`…）
            limit       返回条数上限（默认 200，最多 2000）
        """
        rows = self.analysis.ledger.load()
        if (_q1(query, "picks") or "1") not in ("0", "false", "no"):
            rows = [r for r in rows if r.is_pick]
        st = (_q1(query, "status") or "").strip()
        if st:
            rows = [r for r in rows if r.status == st]
        limit = _q_int(query, "limit", 200, minimum=1, maximum=2000)
        rows = sorted(rows, key=lambda r: r.at, reverse=True)[:limit]
        return {"count": len(rows),
                "entries": [r.as_dict() for r in rows],
                "ledger": self.analysis.ledger.health()}

    def h_ledger_settle(self, query: Mapping[str, List[str]],
                        body: Mapping[str, Any], *_a: str) -> Dict[str, Any]:
        """手动触发一次结算（拉终场比分回填台账）。"""
        return self.analysis.settle_finished()


# --------------------------------------------------------------------------- #
# HTTP 适配壳
# --------------------------------------------------------------------------- #

def create_app(
    snapshot_root: str,
    source: Optional[str] = None,
    saz_path: Optional[str] = None,
    analysis: Optional[Any] = None,
) -> ApiApp:
    """构造 API 应用（供 WSGI/测试使用）。

    source 为空时依次取 `DATA_SOURCE` 环境变量、最后回退 **leyu**（默认数据源）。
    saz_path 给出时乐鱼源进入离线回放（无需联网，供 CI/演示）。
    analysis 可注入已构造的分析服务（含实时推送）；不注入时懒构造。
    """
    # ⚠️ 必须在构造 ValuationService 之前设好会话缓存路径：
    #    数据源（及其 SessionProvider 链）在构造函数里就创建了，
    #    晚于此处设置会导致链上**没有缓存包装**，
    #    表现为「App token 一失效就全停采集」（真实踩过的坑）。
    os.environ.setdefault(
        "LEYU_SESSION_CACHE",
        str(Path(snapshot_root) / ".." / "_session.json"))

    svc = ValuationService(snapshot_root=snapshot_root,
                           source=source,
                           saz_path=saz_path)
    return ApiApp(svc, analysis=analysis)


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


def _start_background(
    svc: ValuationService,
    app: "ApiApp",
) -> Optional[Any]:
    """启动后台实时推送（失败不影响 API 可用）。

    返回 Hub（供关闭时 stop），失败返回 None。
    设计原则：采集是**增强**而非前提——推送挂了，控制台仍应能用快照库工作。
    """
    try:
        from collector.leyu_realtime import RealtimeHub
        from collector.sources import SOCCER_SPORT_ID, SnapshotSource
        from service.analysis import build_analysis_service

        # 显式注解：schedule() 是 SnapshotSource 基类的可选能力，
        # 写清楚类型可让静态检查确认该调用合法（而非靠 getattr 绕过）。
        source: SnapshotSource = svc.source
        provider = getattr(source, "session_provider", None)
        if provider is None:
            print("提示：当前数据源不支持会话，跳过实时推送")
            return None

        # 订阅上限：**默认不截断**（覆盖全部进行中足球）。
        # 用户要求：展示应与乐鱼接口的进行中数量一致，
        # 因此这里不再用固定 60，而是留一个宽松安全阀。
        max_matches = _env_int("REALTIME_MAX_MATCHES", 0, minimum=0)

        def _mids() -> List[str]:
            """订阅源：全部**进行中的足球**赛事（与乐鱼接口一致）。

            委托 `source.live_match_ids()`，避免在 API 层重复写过滤逻辑：
            乐鱼同一网关还返回篮球/网球等（`sport_id != 1`），它们占订阅
            额度且无法映射足球盘口，必须排除；`max_matches` 只是安全阀，
            默认 0（不截断），以与乐鱼页面的进行中数量对齐。

            ## 本地回退（本项目真实故障）

            会话过期时 `live_match_ids()` 抛异常 → 早期实现直接 return []，
            于是 `feed.subscribe_odds([])` 把**一条都没订上**（实测
            `subscribed=0`、`price_ticks=0`），整个实时链路变成空转 ——
            比“订少了”严重得多。

            而磁盘上已有历史走势文件（`_trends/<mid>.jsonl`），
            它们就是“近期真正有过行情的场次”。会话不可用时用这批 mid
            先把订阅建起来，链路就能自愈（拿到新行情后又会持续更新）。
            """
            try:
                live = list(source.live_match_ids(SOCCER_SPORT_ID))
            except Exception as exc:  # noqa: BLE001 - 订阅源失败不终止推送
                print("警告：获取订阅列表失败：%s" % exc)
                if hub.subscribed():
                    raise  # Hub preserves the active subscription on provider failure.
                live = _mids_from_local_trends()
                if live:
                    print("提示：改用本地已知 %d 场建立订阅（会话不可用时的自愈）"
                          % len(live))
                else:
                    raise  # An unavailable source is not an authoritative empty schedule.
            if max_matches > 0:
                live = live[:max_matches]
            return live

        def _mids_from_local_trends() -> List[str]:
            """从本地走势文件反推赛事 ID（会话不可用时的订阅回退）。

            只扫 `_trends/*.jsonl` 的文件名（实测 0.05s 级），
            不读文件内容。
            """
            try:
                root = hub.trend_store.root
                if root is None:
                    return []
                return sorted(p.stem for p in root.glob("*.jsonl"))
            except OSError:
                return []

        # 走势落盘目录：放在快照根下的 _trends/，随 output 卷一起持久化。
        # 这样经济学算法与 LLM 能读到历史走势，容器重启也不丢。
        trend_root = str(Path(svc.store.root) / ".." / "_trends")

        # 先建分析服务（但不启动），以便把它的 notify_price_change
        # 作为 Hub 的盘口变动回调 —— 这是“盘口一变就重算”的接线点。
        # result_path 放在 output 卷内：重启后仍能立即展示上次决策结果。
        result_path = str(Path(svc.store.root) / ".." / "decisions.json")
        ana = build_analysis_service(svc, realtime=None)
        # 注入落盘路径（环境变量优先，否则用默认路径）
        # 台账放在 output 卷内，与快照同生命周期（容器重启不丢）。
        ledger_root = str(Path(svc.store.root) / ".." / "ledger")
        ana.config = replace(ana.config,
                             result_path=ana.config.result_path or result_path,
                             ledger_root=ana.config.ledger_root or ledger_root)
        app._analysis = ana

        hub = RealtimeHub(provider, mids_provider=_mids,
                          max_matches=max_matches, trend_root=trend_root,
                          resume=True,
                          on_state_change=ana.notify_price_change)
        hub.start()
        print("走势持久化: %s" % hub.trend_store.health())

        # 分析服务复用同一个 Hub（用于读走势）
        ana.realtime = hub
        print("实时推送已启动（订阅全部进行中足球%s）"
              % ("，上限 %d 场" % max_matches if max_matches > 0 else ""))

        # **主路径：盘口变动触发决策**（用户要求的触发时机）。
        if ana.start_scheduler():
            print("盘口变动触发决策已启动（防抖 %.0fs，全局最小间隔 %.0fs，"
                  "每批最多 %d 场）"
                  % (ana.config.change_debounce_s,
                     ana.config.change_min_interval_s,
                     ana.config.change_batch))
        else:
            print("提示：盘口变动触发决策未启用（ANALYSIS_CHANGE_TRIGGER=0）")

        # **兜底：定时全量决策**：行情长时间不动或推送断线时保证数据不过期。
        # 页面只读 latest_result()，打开即出结果，不转圈。
        if ana.start_cycle():
            print("定时决策（兜底）已启动（每 %.0fs 一轮，每轮最多 %d 场）"
                  % (ana.config.cycle_interval_s, ana.config.cycle_limit))
        else:
            print("提示：定时决策未启用（ANALYSIS_CYCLE=0）")

        # **赛后结算**：把终场比分回填台账，算命中率/ROI/CLV。
        # 这是回答「LLM 判定准不准」的唯一依据。
        if ana.start_settler():
            print("赛后结算已启动（每 %.0fs 一轮，台账 %s）"
                  % (ana.config.settle_interval_s,
                     ana.ledger.health().get("path")))
        else:
            print("提示：赛后结算未启用（台账不可用或间隔为 0）")
        return hub
    except Exception as exc:  # noqa: BLE001 - 采集失败不能阻止 API 启动
        print("警告：实时推送启动失败（API 仍可用）：%s" % exc)
        return None


def run_server(host: str = "0.0.0.0", port: int = 8000,
               snapshot_root: str = "/app/output/snapshots",
               source: Optional[str] = None,
               saz_path: Optional[str] = None) -> None:
    """启动 API 服务（阻塞）。含后台实时采集 + 定时决策。"""
    app = create_app(snapshot_root, source=source, saz_path=saz_path)
    hub = None
    if os.environ.get("REALTIME_DISABLED", "").strip() not in ("1", "true"):
        hub = _start_background(app.svc, app)
    srv = make_server(app, host, port)
    print("ai_football API 监听 http://%s:%d" % (host, port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ana = getattr(app, "_analysis", None)
        if ana is not None:
            ana.stop_scheduler()
            ana.stop_cycle()
            ana.stop_settler()
        if hub is not None:
            hub.stop()
        srv.server_close()


if __name__ == "__main__":  # pragma: no cover
    run_server(
        host=os.environ.get("API_HOST", "0.0.0.0"),
        port=_env_int("API_PORT", 8000, minimum=1),
        snapshot_root=os.environ.get("SNAPSHOT_ROOT", "/app/output/snapshots"),
        source=os.environ.get("DATA_SOURCE") or None,
        saz_path=os.environ.get("LEYU_SAZ") or None,
    )
