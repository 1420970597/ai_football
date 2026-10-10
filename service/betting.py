#!/usr/bin/env python3
"""Betting gates and Android YBTY single-order protocol (no I/O here)."""
from __future__ import annotations

import math
import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Mapping


class BettingBlocked(ValueError):
    """The configured safety gate rejected an order plan."""


class BettingProtocolError(BettingBlocked):
    """The recommendation does not contain enough App order identifiers."""


VENUE_BET_PATH = "/yewu13/v1/betOrder/client/bet"
ENSEMBLE_ALGORITHM = "economic_ensemble"
EXECUTOR_QUEUE_LIMIT = 256
BLOCKED_RECHECK_S = 30.0


def betting_capability(config: Any) -> dict[str, Any]:
    """Describe the implementation; runtime readiness is added by the executor."""
    return {
        "configured_enabled": bool(getattr(config, "betting_enabled", False)),
        "execution_supported": True,
        "execution_enabled": bool(getattr(config, "betting_enabled", False)),
        "mode": "automatic_single",
        "reason": "开启后自动提交满足门控的经济学综合推荐；关闭后不再提交新订单。",
    }


def _pick_value(source: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = source.get(name)
        if value is not None and value != "":
            return value
    return None


def _required(source: Mapping[str, Any], *names: str) -> Any:
    value = _pick_value(source, *names)
    if value is None:
        raise BettingProtocolError("下注盘口缺少 App 字段：%s" % "/".join(names))
    return value


def _positive_number(value: Any, name: str) -> float:
    result = _number(value, name)
    if result <= 0:
        raise BettingProtocolError("%s 必须大于 0" % name)
    return result


def build_ybty_order_detail(pick: Mapping[str, Any], stake: float) -> dict[str, Any]:
    """Build one ``OBBetRequest.OBOrderDetail`` from App odds metadata.

    The live recommendation normally carries ``details[0]`` from the YBTY
    order DTO.  Keeping the aliases here also lets a caller pass the raw
    ``detailList`` item captured from the App.  No network request is made.
    """
    raw = pick.get("order_detail") or pick.get("detail")
    detail: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    details = pick.get("details")
    if not detail and isinstance(details, list) and details and isinstance(details[0], Mapping):
        detail = details[0]
    merged = dict(detail)
    merged.update({key: value for key, value in pick.items() if value is not None and value != ""})
    odd = _positive_number(_required(merged, "oddFinally", "odds", "odd"), "最终赔率")
    amount = _positive_number(stake, "投注金额")
    result = {
        "betAmount": format(round(amount, 2), ".2f"),
        "matchId": str(_required(merged, "matchId", "match_id")),
        "marketId": str(_required(merged, "marketId", "market_id")),
        "playId": str(_required(merged, "playId", "play_id")),
        "playOptions": str(_required(merged, "playOptions", "play_options", "outcome")),
        "playOptionsId": str(_required(merged, "playOptionsId", "play_options_id", "optionId")),
        "oddFinally": str(odd),
        "odds": str(round(odd * 100000)),
        "marketValue": str(_pick_value(merged, "marketValue", "market_value", "line") or ""),
        "marketTypeFinally": str(_pick_value(merged, "marketTypeFinally", "marketType", "market_type") or "EU"),
        "matchType": int(_pick_value(merged, "matchType", "match_type") or 2),
        "sportId": str(_pick_value(merged, "sportId", "sport_id") or 1),
        "scoreBenchmark": str(_pick_value(merged, "scoreBenchmark", "score_benchmark") or ""),
    }
    for key in ("dataSource", "tournamentId", "tournamentLevel", "matchProcessId",
                "placeNum", "matchName", "matchInfo", "playName", "playOptionName", "sportName", "chpid"):
        if merged.get(key) is not None:
            result[key] = merged[key]
    return result


def build_ybty_bet_payload(pick: Mapping[str, Any], stake: float, *, device_imei: str = "") -> dict[str, Any]:
    """Build the Android App's single-bet request body without submitting it."""
    detail = build_ybty_order_detail(pick, stake)
    detail.pop("chpid", None)
    return {
        "acceptOdds": 2,
        "currencyCode": "CNY",
        "deviceImei": str(device_imei),
        "deviceType": "3",
        "openMiltSingle": 0,
        "preBet": "0",
        "seriesOrders": [{
            "seriesType": "1",
            "seriesSum": 1,
            "fullBet": 0,
            "orderDetailList": [detail],
        }],
    }


@dataclass(frozen=True)
class BetPlan:
    match_id: str
    market: str
    line: str
    outcome: str
    stake: float
    confidence: float
    hit_count: float
    mode: str
    executable: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id, "market": self.market,
            "line": self.line, "outcome": self.outcome,
            "stake": self.stake, "confidence": self.confidence,
            "hit_count": self.hit_count, "mode": self.mode,
            "executable": self.executable,
        }


def _number(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BettingBlocked("%s 不是有效数值" % name) from exc
    if not math.isfinite(result):
        raise BettingBlocked("%s 必须是有限数值" % name)
    return result


def recommendation_confidence(pick: Mapping[str, Any], evidence: Mapping[str, Any], *, stale: bool = False) -> float:
    """Shared board/execution confidence using settled quote evidence."""
    samples = _number(evidence.get("settled_samples", 0) or 0, "已结算样本")
    observed = (_number(evidence.get("hit_count", 0) or 0, "命中次数") + 1) / (samples + 2)
    base = _number(pick.get("confidence") or pick.get("p_model") or 0, "置信度")
    return round(base * (0.5 + 0.5 * observed) * (0.75 if stale else 1.0), 6)


def plan_bet(pick: Mapping[str, Any], config: Any,
             *, match_live: bool = True,
             market_open: bool = True) -> BetPlan:
    """Validate a recommendation and calculate its configured stake.

    ``pick`` must contain the ensemble recommendation's confidence and its
    historical hit count.  Hit count is intentionally checked before stake
    calculation, so a high confidence with no settled evidence cannot result
    in an order plan.
    """
    if not bool(getattr(config, "betting_enabled", False)):
        raise BettingBlocked("投注功能未启用")
    # A provider order may only be produced from the persisted, aggregated
    # recommendation.  Individual algorithm forecasts do not represent the
    # portfolio decision and must never reach the order adapter.
    algorithm = str(pick.get("algorithm") or "")
    if algorithm != ENSEMBLE_ALGORITHM:
        raise BettingBlocked("只允许经济学综合推荐进入投注流程")
    if not match_live:
        raise BettingBlocked("比赛不在进行中")
    if not market_open:
        raise BettingBlocked("盘口已关闭或暂停")
    hit_count = _number(pick.get("hit_count", pick.get("historical_hit_count", 0)), "盘口命中次数")
    minimum = _number(getattr(config, "betting_min_hit_count", 3), "最小命中次数")
    if hit_count < minimum:
        raise BettingBlocked("盘口历史命中次数不足（%.0f/%g）" % (hit_count, minimum))
    confidence = min(1.0, max(0.0, _number(
        pick.get("composite_confidence", pick.get("confidence", 0)), "综合置信度")))
    base = _number(getattr(config, "betting_fixed_stake", 10.0), "固定额度")
    mode = str(getattr(config, "betting_stake_mode", "fixed"))
    if mode == "fixed":
        stake = base
    elif mode == "confidence_multiplier":
        stake = base * confidence
    else:
        raise BettingBlocked("未知投注额度模式")
    if round(stake, 2) <= 0:
        raise BettingBlocked("计算出的投注额度必须大于 0")
    match_id = str(pick.get("match_id") or "")
    market = str(pick.get("market") or "")
    outcome = str(pick.get("outcome") or "")
    if not match_id or not market or not outcome:
        raise BettingBlocked("盘口缺少比赛、玩法或选项标识")
    return BetPlan(match_id, market, str(pick.get("line") or ""), outcome,
                   round(stake, 2), round(confidence, 6), hit_count, mode)


def preview_bet(pick: Mapping[str, Any], config: Any, **kwargs: Any) -> dict[str, Any]:
    """Return a stable UI/API response without submitting an order."""
    execution = betting_capability(config)
    try:
        return {"allowed": True, "plan": plan_bet(pick, config, **kwargs).as_dict(),
                "submitted": False, "submission": "preview_only", "execution": execution}
    except BettingBlocked as exc:
        return {"allowed": False, "reason": str(exc), "plan": None,
                "submitted": False, "submission": "blocked", "execution": execution}


def draft_ybty_bet(pick: Mapping[str, Any], config: Any, **kwargs: Any) -> dict[str, Any]:
    """Create a reviewable App payload; never sends it to the venue.

    The preview/draft APIs never execute. Automatic execution uses only
    server-generated recommendations and performs fresh provider checks.
    """
    try:
        plan = plan_bet(pick, config, **kwargs)
        payload = build_ybty_bet_payload(pick, plan.stake)
        return {
            "allowed": True,
            "submitted": False,
            "execution": betting_capability(config),
            "submission": "manual_only",
            "requires_manual_confirmation": True,
            "provider_path": VENUE_BET_PATH,
            "plan": plan.as_dict(),
            "payload": payload,
        }
    except BettingBlocked as exc:
        return {"allowed": False, "submission": "blocked", "reason": str(exc), "plan": None,
                "submitted": False, "execution": betting_capability(config)}


class BettingExecutor:
    """Single background writer with durable claims before any debit request.

    No network runs on the price/decision thread. Claims survive restarts and
    are shared through SQLite's unique key across processes. A claim is never
    automatically retried, including an interrupted or ambiguous submission.
    """

    def __init__(self, settings: Any, hub: Callable[[], Any], ledger: Callable[[], Any],
                 client_factory: Callable[[], Any] | None = None) -> None:
        self.settings = settings
        self.hub, self.ledger = hub, ledger
        self.client_factory = client_factory
        self.path: Path | None = None
        self._lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._queue: dict[str, dict[str, Any]] = {}
        self._blocked_at: dict[str, float] = {}
        self.last_result: dict[str, Any] = {}

    def bind(self, root: str | None) -> None:
        with self._lock:
            self.path = Path(root).parent / "betting-orders.sqlite3" if root else None

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="bet-order-executor", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(3)

    def configure(self) -> None:
        with self._lock:
            self._queue.clear()
            self._blocked_at.clear()

    def enqueue(self, row: Mapping[str, Any]) -> None:
        cfg, version = self.settings.snapshot()
        if not cfg.betting_enabled or row.get("config_version") != version or not row.get("picks"):
            return
        with self._lock:
            if len(self._queue) >= EXECUTOR_QUEUE_LIMIT:
                self._queue.pop(next(iter(self._queue)))
            self._queue[str(row["match_id"])] = dict(row)

    def _loop(self) -> None:
        while not self._stop.wait(1):
            self.flush()

    def flush(self) -> list[dict[str, Any]]:
        if not self._run_lock.acquire(blocking=False):
            return []
        try:
            with self._lock:
                rows = list(self._queue.values())
                self._queue.clear()
            results = []
            for row in rows:
                for pick in row.get("picks", []):
                    result = self.execute(row, pick)
                    with self._lock:
                        self.last_result = result
                    results.append(result)
            return results
        finally:
            self._run_lock.release()

    def _db(self, path: Path) -> sqlite3.Connection:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=5, isolation_level=None)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("""CREATE TABLE IF NOT EXISTS orders (
                identity TEXT PRIMARY KEY, at REAL NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL)""")
            os.chmod(path, 0o600)
        except (OSError, sqlite3.Error):
            db.close()
            raise
        return db

    def execute(self, row: Mapping[str, Any], pick: Mapping[str, Any]) -> dict[str, Any]:
        """Execute only an in-memory server recommendation, never an API pick."""
        from collector.leyu_account import (BetSubmissionRejected, BetSubmissionUnknown,
                                            account_client_from_env)
        from collector.session import SessionError
        cfg, version = self.settings.snapshot()
        mid = str(row.get("match_id") or "")
        logical = [mid, pick.get("market"), str(pick.get("line") or ""), pick.get("outcome"),
                   str(pick.get("odds") or pick.get("order_detail", {}).get("oddFinally") or "")]
        account = os.environ.get("LEYU_APP_LOGIN_NAME", "").strip().casefold() or "default"
        identity = hashlib.sha256(json.dumps([account, *logical], ensure_ascii=False).encode()).hexdigest()
        result: dict[str, Any] = {"identity": identity, "match_id": mid, "market": pick.get("market"),
                                  "line": pick.get("line"), "outcome": pick.get("outcome"),
                                  "status": "blocked", "submitted": False}
        with self._lock:
            path = self.path
            blocked_at = self._blocked_at.get(identity, 0)
        db: sqlite3.Connection | None = None
        claimed = False
        try:
            if not cfg.betting_enabled:
                raise BettingBlocked("投注功能未启用")
            if path is None:
                raise BettingBlocked("未配置持久化订单目录，无法防止重启重复提交")
            if time.monotonic() - blocked_at < BLOCKED_RECHECK_S:
                raise BettingBlocked("前置校验失败，等待下一次核验")
            if row.get("algorithm") != ENSEMBLE_ALGORITHM or row.get("competition_type") != "real":
                raise BettingBlocked("只执行真实足球的经济学综合推荐")
            if pick not in row.get("picks", []) or pick.get("research_only"):
                raise BettingBlocked("该盘口不是当前综合推荐")
            if row.get("config_version") != version:
                raise BettingBlocked("推荐的设置版本已过期")
            hub = self.hub()
            if hub is None:
                raise BettingBlocked("实时行情未启动")
            snapshot = hub.decision_snapshot(mid)
            if snapshot.get("version") != row.get("version"):
                raise BettingBlocked("实时比赛/盘口已变化，等待重新计算")
            if time.time() * 1000 - float(row.get("published_at_ms") or 0) > cfg.quote_max_age_s * 1000:
                raise BettingBlocked("综合推荐已过期")
            if (snapshot.get("finished") or row.get("finished") or snapshot.get("suspended")
                    or str(snapshot.get("status", {}).get("mmp")) not in ("6", "7")):
                raise BettingBlocked("比赛未进行或盘口暂停")
            for field in ("score_age_s", "status_age_s"):
                age = snapshot.get(field)
                if age is None or not math.isfinite(float(age)) or age > cfg.state_max_age_s:
                    raise BettingBlocked("比分或比赛时钟已过期")
            evidence = self.ledger().recommendation_performance(
                str(pick["market"]), str(pick.get("line") or ""), str(pick["outcome"]))
            confidence = recommendation_confidence(pick, evidence)
            plan = plan_bet({**pick, **evidence, "match_id": mid, "composite_confidence": confidence}, cfg)
            detail = build_ybty_order_detail(pick, plan.stake)
            if detail["matchId"] != mid or detail["sportId"] != "1" or detail["matchType"] != 2:
                raise BettingBlocked("原始订单标识与当前真实足球推荐不一致")
            result["stake"] = plan.stake
            db = self._db(path)
            existing = db.execute("SELECT payload FROM orders WHERE identity=?", (identity,)).fetchone()
            if existing:
                previous = json.loads(existing[0])
                if previous.get("status") == "sending":
                    previous.update(status="unknown", submitted=None, reason="上次提交未取得回执；请核对场馆注单")
                return {**previous, "duplicate": True}
            factory = self.client_factory or account_client_from_env
            client = factory()
            if client is None:
                raise BettingBlocked("未配置乐鱼 App 账户会话")
            detail = client.prepare_bet(detail, plan.stake)
            payload = build_ybty_bet_payload({"order_detail": detail}, plan.stake)
            current, current_version = self.settings.snapshot()
            final = hub.decision_snapshot(mid)
            quote = next((q for q in final.get("quotes", []) if str(q.oid) == detail["playOptionsId"]
                          and str(q.hv or "") == str(pick.get("line") or "")), None)
            if (not current.betting_enabled or current_version != version or self._stop.is_set()
                    or final.get("finished") or final.get("suspended")
                    or final.get("score") != snapshot.get("score")
                    or final.get("status", {}).get("mmp") != snapshot.get("status", {}).get("mmp")
                    or quote is None or quote.quote_age_s > cfg.quote_max_age_s
                    or not math.isclose(quote.odds, float(detail["oddFinally"]), abs_tol=0.000005)
                    or time.time() * 1000 - float(row["published_at_ms"]) > cfg.quote_max_age_s * 1000):
                raise BettingBlocked("提交前配置或行情已变化")
            sending = {**result, "status": "sending", "reason": "已提交发送意图，等待场馆回执"}
            try:
                self.settings.claim_bet(version, lambda: db.execute("INSERT INTO orders VALUES (?,?,?,?)", (
                    identity, time.time(), "sending", json.dumps(sending, ensure_ascii=False))))
            except sqlite3.IntegrityError:
                return {**result, "status": "duplicate", "reason": "该推荐已有订单提交记录"}
            claimed = True
            try:
                result.update(client.submit_bet(payload))
            except BetSubmissionRejected as exc:
                result.update(status="rejected", reason=str(exc))
            except (BetSubmissionUnknown, SessionError) as exc:
                result.update(status="unknown", submitted=None, reason=str(exc))
            db.execute("UPDATE orders SET status=?,payload=? WHERE identity=?", (
                result["status"], json.dumps(result, ensure_ascii=False), identity))
            return result
        except Exception as exc:  # Each failure stops this order, never the worker.
            # No provider exception/body can leak credentials into public state.
            reason = str(exc) if isinstance(exc, (BettingBlocked, SessionError)) else "订单校验或持久化失败"
            if claimed:
                result.update(status="unknown", submitted=None, reason="提交结果未知；请核对场馆注单，不会自动重发")
            else:
                result["reason"] = reason
                with self._lock:
                    if len(self._blocked_at) >= EXECUTOR_QUEUE_LIMIT:
                        self._blocked_at.pop(next(iter(self._blocked_at)))
                    self._blocked_at[identity] = time.monotonic()
            return result
        finally:
            if db is not None:
                db.close()

    def health(self) -> dict[str, Any]:
        cfg, _ = self.settings.snapshot()
        with self._lock:
            running = bool(self._thread and self._thread.is_alive())
            path, last = self.path, dict(self.last_result)
            queued = len(self._queue)
        ready = running and path is not None
        reason = ("投注已关闭" if not cfg.betting_enabled else "执行器未启动" if not running
                  else "未配置持久化订单目录" if path is None else "等待满足门控的实时综合推荐")
        orders: list[dict[str, Any]] = []
        if path and path.exists():
            try:
                with closing(sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True, timeout=1)) as db:
                    for (raw,) in db.execute("SELECT payload FROM orders ORDER BY at DESC LIMIT 20"):
                        order = json.loads(raw)
                        if order.get("status") == "sending":
                            order.update(status="unknown", submitted=None, reason="等待回执或提交被中断；请核对场馆注单")
                        orders.append(order)
            except (OSError, sqlite3.Error, ValueError):
                ready, reason = False, "订单记录不可读取，执行将被阻止"
        return {**betting_capability(cfg), "execution_enabled": cfg.betting_enabled and ready,
                "running": running, "ready": ready, "queued": queued,
                "reason": reason, "last_result": last, "orders": orders}
