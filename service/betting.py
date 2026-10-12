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
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from core.market_labels import describe_market, format_market, parse_market_code
from store.order_store import OrderStore, postgres_configured
from store.sql_backend import SQLiteAdapter, StorageBackendError


class BettingBlocked(ValueError):
    """The configured safety gate rejected an order plan."""


class BettingProtocolError(BettingBlocked):
    """The recommendation does not contain enough App order identifiers."""


class BettingRecomputeNeeded(BettingBlocked):
    """A new decision can resolve this gate without replaying an order."""


class BettingLatencyExceeded(BettingRecomputeNeeded):
    """Backward-compatible name for an obsolete latency-budget failure."""


VENUE_BET_PATH = "/yewu13/v1/betOrder/client/bet"
ENSEMBLE_ALGORITHM = "economic_ensemble"
EXECUTOR_QUEUE_LIMIT = 256
BLOCKED_RECHECK_S = 30.0
LATENCY_TARGET_S = 2.0
# Compatibility alias for callers that imported the old metric name. It is a
# target for telemetry only and is never used to reject a business operation.
EXECUTION_BUDGET_S = LATENCY_TARGET_S
# These waits bound resource contention and persistence confirmation. They are
# transport safeguards, not business deadlines for a selected quote.
SUBMISSION_LOCK_WAIT_S = 15.0
PERSISTENCE_CONFIRM_TIMEOUT_S = 5.0
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
        "reason": "开启后自动提交满足决策出现次数等门控的经济学综合推荐；关闭后不再提交新订单。",
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
    decision_count: float
    mode: str
    executable: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id, "market": self.market,
            "line": self.line, "outcome": self.outcome,
            "stake": self.stake, "confidence": self.confidence,
            # Keep hit_count as a compatibility alias; the execution gate now
            # uses the explicitly named decision occurrence count.
            "decision_count": self.decision_count,
            "hit_count": self.decision_count, "mode": self.mode,
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
    """Shared board/execution confidence using settled outcome evidence."""
    samples = _number(evidence.get("settled_samples", 0) or 0, "已结算样本")
    observed = (_number(evidence.get("hit_count", 0) or 0, "命中次数") + 1) / (samples + 2)
    base = _number(pick.get("confidence") or pick.get("p_model") or 0, "置信度")
    return round(base * (0.5 + 0.5 * observed) * (0.75 if stale else 1.0), 6)


def plan_bet(pick: Mapping[str, Any], config: Any,
             *, match_live: bool = True,
             market_open: bool = True) -> BetPlan:
    """Validate a recommendation and calculate its configured stake.

    ``pick`` must contain the ensemble recommendation's confidence and its
    decision occurrence count.  Settled outcome statistics remain separate
    evidence and are not treated as a decision occurrence.
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
    decision_count = _number(
        pick.get("decision_count", pick.get("decision_occurrences", pick.get("hit_count", 0))),
        "决策出现次数")
    minimum = _number(getattr(config, "betting_min_hit_count", 3), "最小命中次数")
    if decision_count < minimum:
        raise BettingBlocked("投注所需命中次数不足（决策出现次数 %.0f/%g）" % (decision_count, minimum))
    # Recommendations may still be displayed at the lower research threshold.
    # Real orders require a positive, current-quote advantage so a stale or
    # merely high-confidence pick cannot debit the account.
    edge = _pick_value(pick, "effective_ev_live", "effective_ev", "edge")
    if edge is not None:
        minimum_edge = _number(getattr(config, "betting_min_edge", 0.05), "最小下注优势")
        if _number(edge, "当前下注优势") < minimum_edge:
            raise BettingBlocked("当前下注优势不足（%.4f/%g）" % (float(edge), minimum_edge))
    from core.live_model import MODEL_VERSION
    if (pick.get('decision_model_version') or '') != (pick.get('calibration_evidence_version') or ''):
        raise BettingBlocked('校准模型尚无同版本独立结算证据')
    if pick.get('model_version') != MODEL_VERSION:
        raise BettingBlocked('历史证据来自未核验的模型版本')
    # Occurrence count is a stability signal for the current quote, never a
    # substitute for settled performance evidence.  The old decision_count
    # branch skipped these checks, allowing a repeatedly published losing
    # selection to pass the real-money gate without any positive history.
    # Keep both checks mandatory for legacy and current recommendation shapes.
    evidence_minimum = _number(getattr(config, 'betting_min_evidence_matches', 30), '最小独立样本')
    if _number(pick.get('independent_matches', 0), '独立比赛样本') < evidence_minimum:
        raise BettingBlocked('修复后的独立比赛样本不足（%.0f/%g）' %
                             (_number(pick.get('independent_matches', 0), '独立比赛样本'), evidence_minimum))
    if pick.get('roi') is None or _number(pick['roi'], '历史收益率') <= 0:
        raise BettingBlocked('修复后的历史收益率尚未为正')
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
                   round(stake, 2), round(confidence, 6), decision_count, mode)


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
                 on_recompute: Callable[[list[str]], None] | None = None,
                 on_persisted: Callable[[Mapping[str, Any], float], bool] | None = None) -> None:
        self.settings = settings
        self.hub, self.ledger = hub, ledger
        self.client_factory = client_factory
        self.on_recompute = on_recompute
        self.on_persisted = on_persisted
        self._client: Any = None
        self._client_key = ""
        self.path: Path | None = None
        self._root: str | None = None
        self._storage_url = ""
        self._order_store: OrderStore | None = None
        self._lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._submission_lock = threading.Lock()
        self._execution_pool: ThreadPoolExecutor | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._warm_thread: threading.Thread | None = None
        self._warm_wake = threading.Event()
        self._client_build_lock = threading.Lock()
        self._warm_state = 'not_started'
        self._schema_ready = False
        self._submit_latency: deque = deque(maxlen=2048)
        self._receipt_latency: deque = deque(maxlen=2048)
        self._latency_target_misses = 0
        self._submissions = 0
        self._within_budget = 0
        self._queue: dict[str, dict[str, Any]] = {}
        self._latest: dict[str, dict[str, Any]] = {}
        self._retry_due: dict[str, tuple[str, float]] = {}
        self._blocked: dict[str, tuple[float, dict[str, Any]]] = {}
        self._active_executions = 0
        self.last_result: dict[str, Any] = {}

    def bind(self, root: str | None) -> None:
        with self._lock:
            if self._order_store is not None:
                self._order_store.close()
            self._order_store = None
            self._root = root
            self._storage_url = (os.environ.get("BETTING_DB_URL") or
                                 os.environ.get("ORDER_DB_URL") or "").strip()
            if (self._storage_url.startswith(("postgres://", "postgresql://")) or
                    (not self._storage_url and postgres_configured())):
                self.path = None
            elif self._storage_url.startswith("sqlite:///"):
                self.path = Path(self._storage_url[9:]).expanduser()
            else:
                self.path = Path(root).parent / "betting-orders.sqlite3" if root else None
            self._schema_ready = False

    def _ensure_order_store(self) -> OrderStore:
        with self._lock:
            existing = self._order_store
        if existing is not None:
            return existing
        store = OrderStore.from_config(self._root, self._storage_url)
        try:
            store.initialize()
        except BaseException:
            store.close()
            raise
        with self._lock:
            if self._order_store is None:
                self._order_store = store
                self.path = store.path
                self._schema_ready = True
                return store
        store.close()
        assert self._order_store is not None
        return self._order_store

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            if os.environ.get('LEYU_FAST_BETTING', '').lower() in ('1', 'true') and self._execution_pool is None:
                self._execution_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='bet-precheck')
            self._thread = threading.Thread(target=self._loop, name="bet-order-executor", daemon=True)
            self._thread.start()
            if (self.client_factory is None and os.environ.get('LEYU_FAST_BETTING', '').lower() in ('1', 'true')
                    and not (self._warm_thread and self._warm_thread.is_alive())):
                self._warm_thread = threading.Thread(target=self._warm_loop, name='bet-session-warm', daemon=True)
                self._warm_thread.start()

    def _warm_loop(self) -> None:
        from collector.leyu_account import account_client_from_env
        while not self._stop.is_set():
            cfg, _ = self.settings.snapshot()
            if cfg.betting_enabled:
                self._warm_state = 'warming'
                try:
                    client = self._account_client(account_client_from_env)
                    if client is None:
                        self._warm_state = 'unconfigured'
                    else:
                        client.warm_betting()
                        self._warm_state = 'ready'
                    if self._order_store is None and (self.path or self._storage_url):
                        self._ensure_order_store()
                except Exception:
                    self._warm_state = 'retrying'
            else:
                self._warm_state = 'disabled'
            self._warm_wake.wait(10)
            self._warm_wake.clear()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._warm_wake.set()
        self.configure()
        if self._thread:
            self._thread.join(15)
        if self._warm_thread:
            self._warm_thread.join(3)
        if (not (self._thread and self._thread.is_alive())
                and not (self._warm_thread and self._warm_thread.is_alive())):
            with self._lock:
                client, self._client = self._client, None
                self._client_key = ''
            if client is not None and callable(getattr(client, 'close', None)):
                client.close()
            if self._execution_pool:
                self._execution_pool.shutdown(wait=True, cancel_futures=True)
                self._execution_pool = None
            with self._lock:
                store, self._order_store = self._order_store, None
            if store is not None:
                store.close()

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
            for pick in row.get('picks', []):
                identity = _identity([(os.environ.get('LEYU_APP_LOGIN_NAME', '').strip().casefold() or 'default'),
                                      *_bet_selection(mid, pick)])
                blocked = self._blocked.get(identity)
                if (blocked and blocked[1].get('awaiting_new_quote') and
                        float(pick.get('quote_received_at_ms') or 0) >
                        float(blocked[1].get('quote_received_at_ms') or 0)):
                    self._blocked.pop(identity, None)
                    self._retry_due.pop(identity, None)
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
            selections: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
            for mid in mids:
                with self._lock:
                    row = self._queue.pop(mid, None)
                if row is not None:
                    selections.extend((row, pick) for pick in row.get('picks', []))
            def execute_latest(selection):
                row, pick = selection
                try:
                    current_row, current_pick = self._current_pick(row, pick)
                except BettingRecomputeNeeded:
                    return None
                result = self.execute(current_row, current_pick)
                with self._lock:
                    self.last_result = result
                return result
            completed = (self._execution_pool.map(execute_latest, selections) if self._execution_pool
                         else map(execute_latest, selections))
            results = [result for result in completed if result is not None]
            return results
        finally:
            self._run_lock.release()

    def _db(self, path: Path) -> sqlite3.Connection:
        """Compatibility handle for maintenance callers and legacy tests.

        Production reads and writes use ``OrderStore`` directly.  The returned
        SQLite connection is owned by the store and must not be closed by a
        caller while the executor is running.
        """
        store = self._ensure_order_store()
        if store.path != path or not isinstance(store.adapter, SQLiteAdapter):
            raise StorageBackendError("当前订单存储不是该 SQLite 文件")
        return store.adapter.connection

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
        if (snapshot.get("finished") or row.get("finished")
                or str(snapshot.get("status", {}).get("mmp")) not in ("6", "7")):
            raise BettingBlocked("比赛未进行或盘口暂停")
        suspended_ids = {str(value) for value in (snapshot.get('suspended_ids') or ())}
        detail_ids = {str(detail.get('playId') or ''), str(detail.get('chpid') or '')}
        if (snapshot.get('suspended') and not suspended_ids) or suspended_ids.intersection(detail_ids):
            raise BettingBlocked("推荐盘口暂停，等待该盘口的新报价")
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
            if self._client is not None and key == self._client_key:
                return self._client
        # Login/network operations must not hold the enqueue/status lock.
        with self._client_build_lock:
            with self._lock:
                if self._client is not None and key == self._client_key:
                    return self._client
            client = default_factory()
            with self._lock:
                self._client, self._client_key = client, key
            return client

    def _ready_account_client(self, default_factory: Callable[[], Any]) -> Any:
        if self.client_factory is not None or os.environ.get('LEYU_FAST_BETTING', '').lower() not in ('1', 'true'):
            return self._account_client(default_factory)
        key = _identity([k+'='+v for k,v in sorted(os.environ.items()) if k.startswith('LEYU_')])
        with self._lock:
            client = self._client if key == self._client_key else None
        if client is None or not client.betting_ready():
            self._warm_wake.set()
            from collector.leyu_account import BetPreflightRetryable
            raise BetPreflightRetryable('场馆会话尚未预热完成，等待新报价')
        return client

    def execute(self, row: Mapping[str, Any], pick: Mapping[str, Any]) -> dict[str, Any]:
        """Execute only an in-memory server recommendation, never an API pick."""
        from collector.leyu_account import (BetPreflightBudgetExpired, BetPreflightRetryable, BetSubmissionRejected, BetSubmissionUnknown,
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
        received = started
        invalid_clock = False
        try:
            raw_clock = pick.get('quote_received_at')
            if raw_clock is None:
                raw_clock = started-max(0.0, time.time()-float(row.get('published_at_ms') or time.time()*1000)/1000)
            received = float(raw_clock)
            invalid_clock = not math.isfinite(received) or received > started+0.01
        except (TypeError, ValueError, OverflowError):
            invalid_clock = True
        target_ms = int(LATENCY_TARGET_S * 1000)
        result.update(budget_ms=target_ms, latency_target_ms=target_ms,
                      quote_received_at_ms=pick.get('quote_received_at_ms'),
                      source_quote_at_ms=pick.get('ts_ms'),
                      quote_to_execute_ms=round(max(0.0, started-received)*1000, 3),
                      decision_compute_ms=row.get('full_compute_ms'), timings_ms={})
        stage_started = started
        def enter_stage(name: str) -> None:
            nonlocal stage, stage_started
            now = time.monotonic()
            result['timings_ms'][stage] = round((now-stage_started)*1000, 3)
            stage, stage_started = name, now
        with self._lock:
            store = self._order_store
            blocked = self._blocked.get(identity)
        claimed = False
        submit_owned = False
        attempt = 1
        with self._lock:
            self._active_executions += 1
        try:
            if invalid_clock:
                raise BettingBlocked('盘口接收时钟无效')
            if not cfg.betting_enabled:
                raise BettingBlocked("投注功能未启用")
            if store is None:
                try:
                    store = self._ensure_order_store()
                except (StorageBackendError, OSError, sqlite3.Error) as exc:
                    if isinstance(exc, StorageBackendError) and str(exc):
                        raise BettingBlocked(str(exc)) from exc
                    raise BettingBlocked("订单持久化不可用，无法防止重启重复提交") from exc
            if blocked is not None and blocked[1].get('awaiting_new_quote'):
                if (float(pick.get('quote_received_at_ms') or 0) <=
                        float(blocked[1].get('quote_received_at_ms') or 0)):
                    return {**blocked[1], 'recheck_deferred': True}
                # The fresh publication can race with the previous attempt's
                # timeout, so enqueue alone cannot reliably clear this block.
                with self._lock:
                    if self._blocked.get(identity) is blocked:
                        self._blocked.pop(identity, None)
                        self._retry_due.pop(identity, None)
                blocked = None
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
            # The explicit live route is independent from model-worker shadow
            # observation. Algorithm mode keeps its evidence/ROI gates; model
            # routes must have applied an approved artifact.
            model_application = (row.get("ensemble") or {}).get("model_application")
            if "ensemble" in row:
                if not isinstance(model_application, Mapping):
                    raise BettingBlocked("决策模式未通过执行核验，禁止真实下注")
                mode = model_application.get('mode')
                permitted = model_application.get('execution_enabled') is True
                # Preserve the previously verified active-model contract.
                if 'execution_enabled' not in model_application:
                    permitted = mode == 'active' and model_application.get('applied') is True
                if mode in ('model', 'hybrid') and model_application.get('applied') is not True:
                    permitted = False
                if not permitted:
                    raise BettingBlocked("所选决策模式不可用、模型处于影子模式或未通过晋级，禁止真实下注")
            if row.get("config_version") != version:
                raise BettingBlocked("推荐的设置版本已过期")
            decision_mode = str(row.get('decision_mode') or 'algorithm')
            if decision_mode != cfg.decision_mode:
                raise BettingBlocked("推荐的决策模式已变化")
            if isinstance(model_application, Mapping) and 'execution_enabled' in model_application:
                if model_application.get('mode') != decision_mode:
                    raise BettingBlocked("决策执行契约与当前模式不一致")
            if pick.get('decision_mode') and pick.get('decision_mode') != decision_mode:
                raise BettingBlocked("盘口结论与当前决策模式不一致")
            if decision_mode in ('model', 'hybrid'):
                if (not pick.get('decision_model_version') or
                        pick.get('decision_model_version') != row.get('decision_model_version')):
                    raise BettingBlocked("盘口结论与当前模型版本不一致")
            if decision_mode == 'hybrid' and (pick.get('blend_weights') or {}).get('model') != cfg.model_blend_weight:
                raise BettingBlocked("推荐的模型混合权重已变化")
            hub = self.hub()
            if hub is None:
                raise BettingBlocked("实时行情未启动")
            snapshot = hub.decision_snapshot(mid)
            enter_stage('live_quote')
            metadata = {key: (snapshot.get("info") or {}).get(key) or row.get(key)
                        for key in ("home", "away", "league")}
            detail = build_ybty_order_detail({**pick, **metadata}, cfg.betting_fixed_stake)
            self._check_state(row, pick, snapshot, detail, cfg)
            if 'quote_received_at' in pick and pick['quote_received_at'] is None:
                raise BettingRecomputeNeeded('尚未接收到该盘口的新报价，不能使用重启恢复的接收时钟')
            enter_stage('historical_evidence')
            evidence = self.ledger().recommendation_performance(
                str(pick["market"]), str(pick.get("line") or ""), str(pick["outcome"]),
                decision_model_version=str(pick.get("decision_model_version") or ""),
                decision_mode=decision_mode,
                model_blend_weight=cfg.model_blend_weight if decision_mode == 'hybrid' else
                    1.0 if decision_mode == 'model' else 0.0)
            confidence = recommendation_confidence(pick, evidence)
            plan = plan_bet({**pick, **evidence, "match_id": mid, "composite_confidence": confidence}, cfg)
            detail = build_ybty_order_detail({**pick, **metadata}, plan.stake)
            if detail["matchId"] != mid or detail["sportId"] != "1" or detail["matchType"] != 2:
                raise BettingBlocked("原始订单标识与当前真实足球推荐不一致")
            result["stake"] = plan.stake
            previous = store.existing_order(selection_key, account_key)
            if previous is not None:
                return previous
            enter_stage('venue_preflight')
            client = self._ready_account_client(account_client_from_env)
            if client is None:
                raise BettingBlocked("未配置乐鱼 App 账户会话")
            # The latency target is measured below; it is never passed to the
            # provider as an absolute business deadline.
            detail = client.prepare_bet(detail, plan.stake)
            payload = build_ybty_bet_payload({"order_detail": detail}, plan.stake)
            enter_stage('submit_queue')
            if not self._submission_lock.acquire(timeout=SUBMISSION_LOCK_WAIT_S):
                raise BetPreflightRetryable('提交队列繁忙，稍后重试')
            submit_owned = True
            current, current_version = self.settings.snapshot()
            final = hub.decision_snapshot(mid)
            enter_stage('final_quote')
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
            result['publication_id'] = final_row.get('publication_id')
            if self.on_persisted:
                enter_stage('decision_persistence')
                if not self.on_persisted(final_row, PERSISTENCE_CONFIRM_TIMEOUT_S):
                    raise BetPreflightRetryable('完整决策记录未能持久化，无法安全下注')
            enter_stage('durable_claim')
            sending = {**result, "status": "sending", "reason": "已提交发送意图，等待场馆回执"}
            try:
                # Serialize the final duplicate check with the durable claim.
                # Never keep a SQL transaction open during provider calls.
                def authorize_claim(claim: Callable[[], Any]) -> Any:
                    # RuntimeSettings intentionally exposes a void callback;
                    # retain the DB cursor so PostgreSQL/SQLite rowcount=0
                    # conflicts cannot be mistaken for a successful claim.
                    captured: list[Any] = []
                    def invoke() -> Any:
                        value = claim()
                        captured.append(value)
                        return value
                    self.settings.claim_bet(version, invoke)
                    return captured[0] if captured else None

                inserted, previous = store.claim(
                    identity, sending, selection_key, account_key,
                    authorize=authorize_claim)
                if not inserted:
                    return previous or {**result, "status": "duplicate", "duplicate": True,
                                         "reason": "该推荐已有订单提交记录"}
            except (sqlite3.IntegrityError, StorageBackendError):
                return {**result, "status": "blocked", "reason": "订单持久化失败"}
            claimed = True
            enter_stage('submission')
            submission_started = time.monotonic()
            try:
                # The client may record the socket-send timestamp in fast mode,
                # but it always uses its normal network timeout.
                result.update(client.submit_bet(payload))
            except BetSubmissionRejected as exc:
                result.update(status="rejected", reason=str(exc))
            except (BetSubmissionUnknown, SessionError) as exc:
                result.update(status="unknown", submitted=None, reason=str(exc))
            sent = (client.last_submission_sent_at if getattr(client, 'supports_deadline', False) is True
                    else submission_started)
            if sent is not None:
                latency = max(0.0, (sent-received)*1000)
                result.update(quote_to_submit_ms=round(latency, 3), latency_target_met=latency <= target_ms,
                              quote_to_receipt_ms=round((time.monotonic()-received)*1000, 3))
                with self._lock:
                    self._submit_latency.append(latency)
                    self._receipt_latency.append(result['quote_to_receipt_ms'])
                    self._submissions += 1
                    self._within_budget += int(latency <= target_ms)
                    self._latency_target_misses += int(latency > target_ms)
            else:
                result.update(latency_target_met=None, send_time_unknown=True)
            result['timings_ms'][stage] = round((time.monotonic()-stage_started)*1000, 3)
            result.update(stage=stage, elapsed_ms=round((time.monotonic()-started)*1000, 3))
            store.update(identity, result["status"], result)
            with self._lock:
                self._blocked.pop(identity, None)
                self._retry_due.pop(identity, None)
            return result
        except Exception as exc:  # Each failure stops this order, never the worker.
            # No provider exception/body can leak credentials into public state.
            reason = (str(exc) if isinstance(exc, (BettingBlocked, SessionError, StorageBackendError))
                      else "订单校验或持久化失败")
            result.update(stage=stage, elapsed_ms=round((time.monotonic() - started) * 1000, 1))
            result['timings_ms'][stage] = round((time.monotonic()-stage_started)*1000, 3)
            if isinstance(exc, (BettingLatencyExceeded, BetPreflightBudgetExpired)):
                # Kept only for compatibility with older clients. A transport
                # timeout is retryable, but never means that a quote must be
                # discarded solely because the 2-second target was missed.
                result.update(latency_target_met=False)
            if claimed:
                result.update(status="unknown", submitted=None, reason="提交结果未知；请核对场馆注单，不会自动重发")
            else:
                result["reason"] = reason
                recompute = isinstance(exc, BettingRecomputeNeeded) or (
                    isinstance(exc, SessionError) and any(text in reason for text in
                    ("赔率已变化", "盘口线已变化", "找不到推荐的比赛与盘口", "选项已关闭或不存在")))
                result["retry_on_new_decision"] = recompute
                retryable = recompute or isinstance(exc, BetPreflightRetryable)
                if isinstance(exc, BetPreflightRetryable):
                    self._warm_wake.set()
                wait_quote = False
                exhausted = retryable and attempt >= cfg.betting_retry_max_attempts
                delay = (min(BLOCKED_RECHECK_S, cfg.betting_retry_base_delay_s * 2 ** (attempt - 1))
                         if retryable and not exhausted else BLOCKED_RECHECK_S)
                result.update(retryable=retryable, retry_exhausted=exhausted,
                              retry_scheduled=retryable and not exhausted and not wait_quote,
                              awaiting_new_quote=wait_quote, retry_after_s=delay)
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
                    if retryable and not exhausted and not wait_quote:
                        self._retry_due[identity] = (mid, time.monotonic() + delay)
                    else:
                        self._retry_due.pop(identity, None)
                LOGGER.warning("Betting blocked: match_id=%s market=%s line=%s outcome=%s stage=%s elapsed_ms=%s reason=%s",
                               mid, result["market"], result["line"], result["outcome"], stage, result["elapsed_ms"], reason)
            return result
        finally:
            if submit_owned:
                self._submission_lock.release()
            # Synchronous maintenance/tests do not own a worker thread. Close
            # their idle store after the operation; the live background loop
            # keeps one connection for PostgreSQL latency and claim locking.
            with self._lock:
                self._active_executions = max(0, self._active_executions - 1)
                idle_store = (self._order_store
                              if self._active_executions == 0
                              and not (self._thread and self._thread.is_alive()) else None)
                if idle_store is not None:
                    self._order_store = None
            if idle_store is not None:
                idle_store.close()

    def statuses(self, selections: list[tuple[str, str, str, str]]) -> dict[tuple[str, str, str, str], dict[str, Any]]:
        """Read execution evidence for each visible pick without venue calls."""
        wanted = {_identity(list(key)): key for key in selections}
        account = os.environ.get("LEYU_APP_LOGIN_NAME", "").strip().casefold() or "default"
        account_key = _identity([account])
        with self._lock:
            store = self._order_store
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
        if not wanted:
            return out
        try:
            if store is None:
                store = self._ensure_order_store()
            for order in store.orders_for_selections(list(wanted), account_key):
                parts = _bet_selection(str(order.get("match_id") or ""), order)
                key = (parts[0], parts[1], parts[2], parts[3])
                if key in out:
                    out[key] = order
        except (OSError, sqlite3.Error, StorageBackendError, ValueError):
            for key in out:
                out[key] = {"status": "blocked", "submitted": False, "reason": "订单记录不可读取"}
        return out

    def health(self) -> dict[str, Any]:
        from service.live_expert import percentiles
        cfg, _ = self.settings.snapshot()
        with self._lock:
            running = bool(self._thread and self._thread.is_alive())
            store, last = self._order_store, dict(self.last_result)
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
        if store is None and running and (self.path or self._storage_url):
            try:
                store = self._ensure_order_store()
            except (OSError, sqlite3.Error, StorageBackendError):
                store = None
        ready = running and store is not None
        reason = ("投注已关闭" if not cfg.betting_enabled else "执行器未启动" if not running
                  else "未配置持久化订单目录" if store is None else "等待满足门控的实时综合推荐")
        orders: list[dict[str, Any]] = []
        if store is not None:
            try:
                orders = store.recent(20)
            except (OSError, sqlite3.Error, StorageBackendError, ValueError):
                ready, reason = False, "订单记录不可读取，执行将被阻止"
        return {**betting_capability(cfg), "execution_enabled": cfg.betting_enabled and ready,
                "running": running, "ready": ready, "queued": queued,
                "retry_pending": retry_pending,
                "retry_policy": {"max_attempts": cfg.betting_retry_max_attempts,
                                 "base_delay_s": cfg.betting_retry_base_delay_s},
                'session_warmup': self._warm_state,
                'latency': {'budget_ms': int(LATENCY_TARGET_S * 1000),
                            'latency_target_ms': int(LATENCY_TARGET_S * 1000),
                            'basis': 'selected_quote_receive_to_socket_send',
                            'submissions': self._submissions, 'within_budget': self._within_budget,
                            'target_misses': self._latency_target_misses,
                            # Kept for older dashboards; this is no longer a
                            # count of orders blocked by the latency target.
                            'deadline_blocked': 0,
                            'quote_to_submit_ms': percentiles(list(self._submit_latency)),
                            'quote_to_receipt_ms': percentiles(list(self._receipt_latency))},
                "reason": reason, "last_result": last, "orders": orders}
