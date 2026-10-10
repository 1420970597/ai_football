#!/usr/bin/env python3
"""Betting gates and controlled Android YBTY single-order execution."""
from __future__ import annotations

import math
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Mapping

from core.market_labels import describe_market, format_market, parse_market_code


class BettingBlocked(ValueError):
    """The configured safety gate rejected an order plan."""


class BettingProtocolError(BettingBlocked):
    """The recommendation does not contain enough App order identifiers."""


class BettingRecomputeNeeded(BettingBlocked):
    """A new decision can resolve this gate without replaying an order."""


VENUE_BET_PATH = "/yewu13/v1/betOrder/client/bet"
ENSEMBLE_ALGORITHM = "economic_ensemble"
EXECUTOR_QUEUE_LIMIT = 256
BLOCKED_RECHECK_S = 30.0
LOGGER = logging.getLogger(__name__)


def _bet_selection(match_id: str, pick: Mapping[str, Any]) -> list[str]:
    """Logical selection only: prices, quote versions and provider IDs may change."""
    return [match_id, str(pick.get("market") or ""),
            str(pick["line"] if pick.get("line") is not None else ""),
            str(pick.get("outcome") or "")]


def _identity(parts: list[str]) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


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
    # The venue retains these display strings with the order. Changing the
    # query language later does not rewrite names stored on an old receipt.
    names = {}
    home, away, league = (str(pick.get(key) or "") for key in ("home", "away", "league"))
    if home and away:
        names["matchInfo"] = home + " v " + away
    if league:
        names["matchName"] = league
    market = str(pick.get("market") or "")
    parsed = parse_market_code(market)
    if parsed:
        family, half, _ = parsed
        code = family + ("_1H" if half else "")
        names["playName"] = describe_market(code)
        if pick.get("outcome"):
            names["playOptionName"] = format_market(market, pick["outcome"], result["marketValue"], home, away)
    if result["sportId"] == "1":
        names["sportName"] = "足球"
    for key, value in names.items():
        if (any("\u4e00" <= c <= "\u9fff" for c in value)
                and not any("\u4e00" <= c <= "\u9fff" for c in str(result.get(key) or ""))):
            result[key] = value
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
                 client_factory: Callable[[], Any] | None = None,
                 on_recompute: Callable[[list[str]], None] | None = None) -> None:
        self.settings = settings
        self.hub, self.ledger = hub, ledger
        self.client_factory = client_factory
        self.on_recompute = on_recompute
        self._client: Any = None
        self._client_key = ""
        self.path: Path | None = None
        self._lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._queue: dict[str, dict[str, Any]] = {}
        self._latest: dict[str, dict[str, Any]] = {}
        self._retry_due: dict[str, tuple[str, float]] = {}
        self._blocked: dict[str, tuple[float, dict[str, Any]]] = {}
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
        self._wake.set()
        self.configure()
        if self._thread:
            self._thread.join(3)

    def configure(self) -> None:
        with self._lock:
            self._queue.clear()
            self._latest.clear()
            self._retry_due.clear()
            self._blocked.clear()

    def enqueue(self, row: Mapping[str, Any]) -> None:
        cfg, version = self.settings.snapshot()
        if not cfg.betting_enabled or row.get("config_version") != version:
            return
        with self._lock:
            mid = str(row["match_id"])
            previous = self._latest.get(mid)
            if previous and float(previous.get("published_at_ms") or 0) > float(row.get("published_at_ms") or 0):
                return
            if mid not in self._latest and len(self._latest) >= EXECUTOR_QUEUE_LIMIT:
                self._latest.pop(next(iter(self._latest)))
            self._latest[mid] = dict(row)
            selections = {_identity(_bet_selection(mid, p)) for p in row.get("picks", [])}
            for identity, (_, blocked_result) in list(self._blocked.items()):
                if blocked_result.get("match_id") == mid and _identity(_bet_selection(mid, blocked_result)) not in selections:
                    self._retry_due.pop(identity, None)
                    self._blocked.pop(identity, None)
            if not row.get("picks"):
                self._queue.pop(mid, None)
                return
            if len(self._queue) >= EXECUTOR_QUEUE_LIMIT:
                self._queue.pop(next(iter(self._queue)))
            self._queue[mid] = dict(row)
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(0.25)
            self._wake.clear()
            if self._stop.is_set():
                return
            self.flush()

    def _request_due_retries(self) -> None:
        with self._lock:
            due = [(identity, mid) for identity, (mid, at) in self._retry_due.items()
                   if at <= time.monotonic()]
            for identity, _ in due:
                self._retry_due.pop(identity, None)
                if identity in self._blocked:
                    self._blocked[identity][1].update(retry_scheduled=False, awaiting_new_decision=True)
        if due and self.on_recompute:
            try:
                self.on_recompute(list(dict.fromkeys(mid for _, mid in due)))
            except Exception:
                # Retain the timer if notification failed; never replay its old row.
                with self._lock:
                    for identity, mid in due:
                        if identity in self._blocked:
                            self._retry_due[identity] = (mid, time.monotonic() + 1)

    def _current_pick(self, row: Mapping[str, Any], pick: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        mid = str(row.get("match_id") or "")
        with self._lock:
            latest = self._latest.get(mid, row)
        selection = _bet_selection(mid, pick)
        for candidate in latest.get("picks", []):
            if _bet_selection(mid, candidate) == selection:
                return latest, candidate
        raise BettingRecomputeNeeded("最新综合决策已撤回该推荐")

    def flush(self) -> list[dict[str, Any]]:
        if not self._run_lock.acquire(blocking=False):
            return []
        try:
            self._request_due_retries()
            with self._lock:
                mids = list(self._queue)
            results = []
            for mid in mids:
                with self._lock:
                    row = self._queue.pop(mid, None)
                if row is None:
                    continue
                for pick in row.get("picks", []):
                    try:
                        current_row, current_pick = self._current_pick(row, pick)
                    except BettingRecomputeNeeded:
                        continue
                    result = self.execute(current_row, current_pick)
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
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS orders (
                identity TEXT PRIMARY KEY, at REAL NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
                selection_key TEXT NOT NULL, account_key TEXT)""")
            columns = {col[1] for col in db.execute("PRAGMA table_info(orders)")}
            for name in ("selection_key", "account_key"):
                if name not in columns:
                    db.execute("ALTER TABLE orders ADD COLUMN " + name + " TEXT")
            for old_identity, raw in db.execute("SELECT identity,payload FROM orders WHERE selection_key IS NULL").fetchall():
                previous = json.loads(raw)
                if not isinstance(previous, dict) or not all(previous.get(k) for k in ("match_id", "market", "outcome")):
                    raise BettingBlocked("历史订单标识不完整，无法安全去重")
                # Legacy payloads did not retain account/odds. Keep every receipt
                # and conservatively reserve this selection for every account.
                selection = _identity(_bet_selection(str(previous["match_id"]), previous))
                db.execute("UPDATE orders SET selection_key=? WHERE identity=?", (selection, old_identity))
            db.execute("CREATE INDEX IF NOT EXISTS orders_selection ON orders(selection_key,account_key)")
            os.chmod(path, 0o600)
            db.commit()
        except BaseException:
            db.rollback()
            db.close()
            raise
        return db

    def _existing_order(self, db: sqlite3.Connection, selection_key: str,
                        account_key: str) -> dict[str, Any] | None:
        existing = db.execute("""SELECT payload FROM orders WHERE selection_key=?
            AND (account_key=? OR account_key IS NULL) ORDER BY at DESC,identity LIMIT 1""",
                              (selection_key, account_key)).fetchone()
        if existing is None:
            return None
        previous = json.loads(existing[0])
        if previous.get("status") == "sending":
            previous.update(status="unknown", submitted=None, reason="上次提交未取得回执；请核对场馆注单")
        return {**previous, "duplicate": True}

    def _check_state(self, row: Mapping[str, Any], pick: Mapping[str, Any],
                     snapshot: Mapping[str, Any], detail: Mapping[str, Any], cfg: Any,
                     *, original: Mapping[str, Any] | None = None) -> None:
        """Validate the selected quote and match state, not a global event counter.

        C105 refreshes the counter even for unchanged prices. Score and phase
        belong to the decision; timestamps and unrelated quote updates do not.
        """
        decision_age = (time.time() * 1000 - float(row.get("published_at_ms") or 0)) / 1000
        if not math.isfinite(decision_age) or decision_age < -1 or decision_age > cfg.quote_max_age_s:
            raise BettingRecomputeNeeded("综合推荐已过期，需要重新计算")
        if (snapshot.get("finished") or row.get("finished") or snapshot.get("suspended")
                or str(snapshot.get("status", {}).get("mmp")) not in ("6", "7")):
            raise BettingBlocked("比赛未进行或盘口暂停")
        for field in ("score_age_s", "status_age_s"):
            age = snapshot.get(field)
            if age is None or not math.isfinite(float(age)) or float(age) > cfg.state_max_age_s:
                raise BettingRecomputeNeeded("比分或比赛时钟已过期")
        expected = original if original is not None else row
        expected_score = expected.get("score")
        expected_phase = (expected.get("status", {}).get("mmp") if original is not None
                          else expected.get("phase"))
        actual_score = snapshot.get("score")
        if (expected_score is None or actual_score is None or tuple(expected_score) != tuple(actual_score)
                or str(expected_phase) != str(snapshot.get("status", {}).get("mmp"))):
            raise BettingRecomputeNeeded("比分或比赛阶段已变化，需要重新计算")
        matches = []
        for quote in snapshot.get("quotes", []):
            native = getattr(quote, "order_detail", {})
            if (str(quote.oid) != str(detail["playOptionsId"])
                    or str(quote.hv or "") != str(pick.get("line") or "")
                    or str(quote.ot) != str(detail["playOptions"])
                    or str(quote.chpid) != str(detail.get("chpid") or detail["playId"])):
                continue
            # Native identifiers prevent matching another half/market with
            # a reused option ID. Older local quotes have no native metadata.
            if any(native.get(key) not in (None, "", detail[key])
                   and str(native[key]) != str(detail[key]) for key in ("matchId", "marketId", "playId")):
                continue
            matches.append(quote)
        if not matches:
            raise BettingRecomputeNeeded("推荐盘口或选项已更新，需要重新计算")
        quote = max(matches, key=lambda q: q.ts_ms)
        if not math.isfinite(quote.quote_age_s) or quote.quote_age_s > cfg.quote_max_age_s:
            raise BettingRecomputeNeeded("推荐盘口报价已过期，需要重新计算")
        if not math.isclose(quote.odds, float(detail["oddFinally"]), abs_tol=0.000005):
            raise BettingRecomputeNeeded("推荐盘口赔率已变化，需要重新计算")

    def _account_client(self, default_factory: Callable[[], Any]) -> Any:
        if self.client_factory is not None:
            return self.client_factory()
        # Keep authentication within the executor. Recreating the client for
        # every pick used to repeat venue launch and consume quote validity.
        key = _identity([k + "=" + v for k, v in sorted(os.environ.items()) if k.startswith("LEYU_")])
        with self._lock:
            if self._client is None or key != self._client_key:
                self._client = default_factory()
                self._client_key = key
            return self._client

    def execute(self, row: Mapping[str, Any], pick: Mapping[str, Any]) -> dict[str, Any]:
        """Execute only an in-memory server recommendation, never an API pick."""
        from collector.leyu_account import (BetPreflightRetryable, BetSubmissionRejected, BetSubmissionUnknown,
                                            account_client_from_env)
        from collector.session import SessionError
        cfg, version = self.settings.snapshot()
        mid = str(row.get("match_id") or "")
        logical = _bet_selection(mid, pick)
        account = os.environ.get("LEYU_APP_LOGIN_NAME", "").strip().casefold() or "default"
        identity = _identity([account, *logical])
        selection_key, account_key = _identity(logical), _identity([account])
        result: dict[str, Any] = {"identity": identity, "match_id": mid, "market": pick.get("market"),
                                  "line": pick.get("line"), "outcome": pick.get("outcome"),
                                  "status": "blocked", "submitted": False,
                                  "checked_at_ms": int(time.time() * 1000)}
        result["decision_at_ms"] = row.get("published_at_ms")
        stage = "recommendation"
        started = time.monotonic()
        with self._lock:
            path = self.path
            blocked = self._blocked.get(identity)
        db: sqlite3.Connection | None = None
        claimed = False
        attempt = 1
        try:
            if not cfg.betting_enabled:
                raise BettingBlocked("投注功能未启用")
            if path is None:
                raise BettingBlocked("未配置持久化订单目录，无法防止重启重复提交")
            if blocked is not None:
                remaining = blocked[0] + float(blocked[1].get("retry_after_s", BLOCKED_RECHECK_S)) - time.monotonic()
                if remaining > 0:
                    # Repeated recommendations must not restart the cooldown
                    # or overwrite the failure that explains why no order was sent.
                    return {**blocked[1], "recheck_deferred": True,
                            "retry_after_s": round(remaining, 3)}
                if blocked[1].get("retryable") and not blocked[1].get("retry_exhausted"):
                    attempt = int(blocked[1].get("attempt", 1)) + 1
                if (blocked[1].get("retryable") and
                        float(row.get("published_at_ms") or 0) <= float(blocked[1].get("decision_at_ms") or 0)):
                    return {**blocked[1], "recheck_deferred": True, "awaiting_new_decision": True}
            result["attempt"] = attempt
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
            stage = "live_quote"
            metadata = {key: (snapshot.get("info") or {}).get(key) or row.get(key)
                        for key in ("home", "away", "league")}
            detail = build_ybty_order_detail({**pick, **metadata}, cfg.betting_fixed_stake)
            self._check_state(row, pick, snapshot, detail, cfg)
            stage = "historical_evidence"
            evidence = self.ledger().recommendation_performance(
                str(pick["market"]), str(pick.get("line") or ""), str(pick["outcome"]))
            confidence = recommendation_confidence(pick, evidence)
            plan = plan_bet({**pick, **evidence, "match_id": mid, "composite_confidence": confidence}, cfg)
            detail = build_ybty_order_detail({**pick, **metadata}, plan.stake)
            if detail["matchId"] != mid or detail["sportId"] != "1" or detail["matchType"] != 2:
                raise BettingBlocked("原始订单标识与当前真实足球推荐不一致")
            result["stake"] = plan.stake
            db = self._db(path)
            previous = self._existing_order(db, selection_key, account_key)
            if previous is not None:
                return previous
            stage = "venue_preflight"
            client = self._account_client(account_client_from_env)
            if client is None:
                raise BettingBlocked("未配置乐鱼 App 账户会话")
            detail = client.prepare_bet(detail, plan.stake)
            payload = build_ybty_bet_payload({"order_detail": detail}, plan.stake)
            current, current_version = self.settings.snapshot()
            final = hub.decision_snapshot(mid)
            stage = "final_quote"
            if not current.betting_enabled or current_version != version or self._stop.is_set():
                raise BettingBlocked("提交前投注配置已变化或执行器已停止")
            final_row, final_pick = self._current_pick(row, pick)
            # A newer decision may renew validity only if it still recommends
            # exactly the same native quote and odds checked by the venue.
            latest_detail = build_ybty_order_detail(final_pick, plan.stake)
            if any(str(latest_detail[k]) != str(detail[k]) for k in
                   ("matchId", "marketId", "playId", "playOptionsId", "playOptions", "marketValue", "oddFinally")):
                raise BettingRecomputeNeeded("最新综合决策的报价已变化，需要重新校验")
            latest_plan = plan_bet({**final_pick, **evidence, "match_id": mid,
                                   "composite_confidence": recommendation_confidence(final_pick, evidence)}, cfg)
            if latest_plan.stake != plan.stake:
                raise BettingRecomputeNeeded("最新综合决策的投注额度已变化，需要重新校验")
            self._check_state(final_row, final_pick, final, detail, cfg, original=snapshot)
            result["decision_at_ms"] = final_row.get("published_at_ms")
            stage = "durable_claim"
            sending = {**result, "status": "sending", "reason": "已提交发送意图，等待场馆回执"}
            try:
                # Serialize the final duplicate check with the durable claim.
                # Never keep a SQLite transaction open during provider calls.
                db.execute("BEGIN IMMEDIATE")
                previous = self._existing_order(db, selection_key, account_key)
                if previous is not None:
                    db.rollback()
                    return previous
                self.settings.claim_bet(version, lambda: db.execute("""INSERT INTO orders
                    (identity,at,status,payload,selection_key,account_key) VALUES (?,?,?,?,?,?)""", (
                    identity, time.time(), "sending", json.dumps(sending, ensure_ascii=False),
                    selection_key, account_key)))
                db.commit()
            except sqlite3.IntegrityError:
                db.rollback()
                return {**result, "status": "duplicate", "duplicate": True, "reason": "该推荐已有订单提交记录"}
            claimed = True
            stage = "submission"
            try:
                result.update(client.submit_bet(payload))
            except BetSubmissionRejected as exc:
                result.update(status="rejected", reason=str(exc))
            except (BetSubmissionUnknown, SessionError) as exc:
                result.update(status="unknown", submitted=None, reason=str(exc))
            db.execute("UPDATE orders SET status=?,payload=? WHERE identity=?", (
                result["status"], json.dumps(result, ensure_ascii=False), identity))
            with self._lock:
                self._blocked.pop(identity, None)
                self._retry_due.pop(identity, None)
            return result
        except Exception as exc:  # Each failure stops this order, never the worker.
            # No provider exception/body can leak credentials into public state.
            reason = str(exc) if isinstance(exc, (BettingBlocked, SessionError)) else "订单校验或持久化失败"
            result.update(stage=stage, elapsed_ms=round((time.monotonic() - started) * 1000, 1))
            if claimed:
                result.update(status="unknown", submitted=None, reason="提交结果未知；请核对场馆注单，不会自动重发")
            else:
                result["reason"] = reason
                recompute = isinstance(exc, BettingRecomputeNeeded) or (
                    isinstance(exc, SessionError) and any(text in reason for text in
                    ("赔率已变化", "盘口线已变化", "找不到推荐的比赛与盘口", "选项已关闭或不存在")))
                result["retry_on_new_decision"] = recompute
                retryable = recompute or isinstance(exc, BetPreflightRetryable)
                exhausted = retryable and attempt >= cfg.betting_retry_max_attempts
                delay = (min(BLOCKED_RECHECK_S, cfg.betting_retry_base_delay_s * 2 ** (attempt - 1))
                         if retryable and not exhausted else BLOCKED_RECHECK_S)
                result.update(retryable=retryable, retry_exhausted=exhausted,
                              retry_scheduled=retryable and not exhausted, retry_after_s=delay)
                current_cfg, current_version = self.settings.snapshot()
                with self._lock:
                    latest = self._latest.get(mid)
                    still_selected = latest is None or any(_bet_selection(mid, p) == logical
                                                           for p in latest.get("picks", []))
                    if (not still_selected or not current_cfg.betting_enabled
                            or current_version != version or self._stop.is_set()):
                        self._blocked.pop(identity, None)
                        self._retry_due.pop(identity, None)
                        result.update(retry_scheduled=False, retry_cancelled=True)
                        LOGGER.info("Betting retry cancelled: match_id=%s stage=%s reason=%s", mid, stage, reason)
                        return result
                    if identity not in self._blocked and len(self._blocked) >= EXECUTOR_QUEUE_LIMIT:
                        evicted = next(iter(self._blocked))
                        self._blocked.pop(evicted)
                        self._retry_due.pop(evicted, None)
                    self._blocked[identity] = (time.monotonic(), dict(result))
                    if retryable and not exhausted:
                        self._retry_due[identity] = (mid, time.monotonic() + delay)
                    else:
                        self._retry_due.pop(identity, None)
                LOGGER.warning("Betting blocked: match_id=%s market=%s line=%s outcome=%s stage=%s elapsed_ms=%s reason=%s",
                               mid, result["market"], result["line"], result["outcome"], stage, result["elapsed_ms"], reason)
            return result
        finally:
            if db is not None:
                db.close()

    def statuses(self, selections: list[tuple[str, str, str, str]]) -> dict[tuple[str, str, str, str], dict[str, Any]]:
        """Read execution evidence for each visible pick without venue calls."""
        wanted = {_identity(list(key)): key for key in selections}
        account = os.environ.get("LEYU_APP_LOGIN_NAME", "").strip().casefold() or "default"
        account_key = _identity([account])
        with self._lock:
            path = self.path
            attempts = [dict(item[1]) for item in self._blocked.values()]
            queued = dict(self._queue)
        cfg, _ = self.settings.snapshot()
        out: dict[tuple[str, str, str, str], dict[str, Any]] = {key: {"status": "not_submitted" if cfg.betting_enabled else "disabled",
                     "submitted": False, "reason": "尚无订单提交记录" if cfg.betting_enabled else "投注功能未启用"}
               for key in selections}
        for mid, row in queued.items():
            for pick in row.get("picks", []):
                parts = _bet_selection(mid, pick)
                key = (parts[0], parts[1], parts[2], parts[3])
                if key in out:
                    out[key] = {"status": "queued", "submitted": False, "reason": "等待执行检查"}
        for attempt in attempts:
            parts = _bet_selection(str(attempt.get("match_id") or ""), attempt)
            key = (parts[0], parts[1], parts[2], parts[3])
            if key in out:
                out[key] = attempt
        if not path or not path.exists() or not wanted:
            return out
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                columns = {col[1] for col in db.execute("PRAGMA table_info(orders)")}
                # GET must not migrate an old DB; execution owns that transaction.
                if "selection_key" not in columns:
                    rows = db.execute("SELECT payload FROM orders ORDER BY at").fetchall()
                else:
                    hashes = list(wanted)
                    rows = []
                    for offset in range(0, len(hashes), 200):
                        batch = hashes[offset:offset+200]
                        rows.extend(db.execute("SELECT payload FROM orders WHERE selection_key IN (" +
                            ",".join("?" for _ in batch) + ") AND (account_key=? OR account_key IS NULL) ORDER BY at",
                            [*batch, account_key]).fetchall())
                for (raw,) in rows:
                    order = json.loads(raw)
                    parts = _bet_selection(str(order.get("match_id") or ""), order)
                    key = (parts[0], parts[1], parts[2], parts[3])
                    if key not in out:
                        continue
                    if order.get("status") == "sending":
                        order.update(status="unknown", submitted=None, reason="发送意图已有记录，等待或核对场馆回执")
                    out[key] = order
        except (OSError, sqlite3.Error, ValueError):
            for key in out:
                out[key] = {"status": "blocked", "submitted": False, "reason": "订单记录不可读取"}
        return out

    def health(self) -> dict[str, Any]:
        cfg, _ = self.settings.snapshot()
        with self._lock:
            running = bool(self._thread and self._thread.is_alive())
            path, last = self.path, dict(self.last_result)
            blocked = self._blocked.get(str(last.get('identity') or ''))
            if blocked:
                last.update(blocked[1])
            elif last.get('retry_scheduled') or last.get('awaiting_new_decision'):
                # The historical failure remains useful, but a withdrawn,
                # cleared or evicted retry must not look like an active timer.
                last.update(retry_scheduled=False, awaiting_new_decision=False,
                            retry_cancelled=True)
            queued = len(self._queue)
            retry_pending = len(self._retry_due)
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
                "retry_pending": retry_pending,
                "retry_policy": {"max_attempts": cfg.betting_retry_max_attempts,
                                 "base_delay_s": cfg.betting_retry_base_delay_s},
                "reason": reason, "last_result": last, "orders": orders}
