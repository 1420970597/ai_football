from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os
import signal
import time
from typing import Any, Mapping

import httpx

from .leyu_protocol import LeyuError, RateLimited
from .leyu_health import ThresholdAlert
from .leyu_realtime import LeyuRealtime
from .leyu_runtime import LeyuRuntime
from .telegram import TelegramCollector

log = logging.getLogger("platform.collector")
API_URL = os.environ.get("PLATFORM_API", "http://api:8080/internal").rstrip("/")
TOKEN = os.environ.get("COLLECTOR_INTERNAL_TOKEN", "")
INTERVAL = max(10, min(300, int(os.environ.get("COLLECTOR_INTERVAL_SECONDS", "20"))))
LIVE_ODDS_REFRESH_SECONDS = max(10, min(120, int(os.environ.get("LIVE_ODDS_REFRESH_SECONDS", "20"))))
PREGAME_NEAR_HOURS = max(1, min(12, int(os.environ.get("PREGAME_NEAR_HOURS", "2"))))
PREGAME_NEAR_REFRESH_SECONDS = max(30, min(600, int(os.environ.get("PREGAME_NEAR_REFRESH_SECONDS", "60"))))
PREGAME_FAR_REFRESH_SECONDS = max(60, min(1800, int(os.environ.get("PREGAME_FAR_REFRESH_SECONDS", "300"))))
ODDS_FIXTURE_BUDGET = max(12, min(300, int(os.environ.get("ODDS_FIXTURE_BUDGET", "96"))))
RATE_LIMIT_COOLDOWN_SECONDS = 60
MAX_RATE_LIMIT_COOLDOWN_SECONDS = 900


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # HTTP客户端INFO会记录完整URL，提取代理地址的查询串可能含密码或key。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # TypeNotFound 的诊断会把未解析的 MTProto 字节（可能包含消息正文）写入日志。
    logging.getLogger("telethon.network.mtprotosender").setLevel(logging.WARNING)


async def get_config(client: httpx.AsyncClient, api_url: str = API_URL) -> dict[str, str]:
    response = await client.get(f"{api_url}/collector/config")
    response.raise_for_status()
    document = response.json()
    if not isinstance(document, dict) or not isinstance(document.get("items"), dict):
        raise LeyuError("invalid_collector_configuration")
    return document["items"]


def category(error: BaseException) -> str:
    return error.category if isinstance(error, LeyuError) else type(error).__name__


def failure_group(error: BaseException, *, stage: str = "source") -> str:
    """Stable, redacted failure family; ingestion refusal is not upstream refusal."""
    if stage == "ingestion":
        return "ingestion"
    value = category(error)
    if isinstance(error, RateLimited):
        return "rate_limit"
    if value in {"session_expired", "authorized_session_missing"}:
        return "session"
    if value in {"permission_denied", "http_access_denied"}:
        return "permission"
    if value == "upstream_domain_unresolved":
        return "domain"
    if value.startswith(("invalid_", "response_", "encoded_", "decoded_")):
        return "configuration" if any(part in value for part in ("config", "base_url", "budget")) else "protocol"
    if "timeout" in value.lower() or "transport" in value.lower() or isinstance(error, (httpx.TransportError, OSError)):
        return "network"
    if value.endswith("_missing") or value == "configuration_changed":
        return "configuration"
    return "source" if isinstance(error, LeyuError) else "internal"


def collection_retry_delay(interval: float, failures: int, *, rate_limit_streak: int = 0,
                           retry_after: int | None = None) -> float:
    if rate_limit_streak:
        # Consecutive limits lengthen the cooldown; a push cannot wake this wait.
        requested = retry_after if type(retry_after) is int else 0
        return min(MAX_RATE_LIMIT_COOLDOWN_SECONDS, max(interval, requested,
                   RATE_LIMIT_COOLDOWN_SECONDS * 2 ** min(max(rate_limit_streak - 1, 0), 4)))
    return interval if failures == 0 else min(60, 2 ** min(failures, 6))


async def wait_for_work(stop: asyncio.Event, delay: float, wake: asyncio.Event | None = None) -> None:
    tasks = [asyncio.create_task(stop.wait())]
    if wake is not None:
        tasks.append(asyncio.create_task(wake.wait()))
    try:
        await asyncio.wait(tasks, timeout=max(0, delay), return_when=asyncio.FIRST_COMPLETED)
        if wake is not None and wake.is_set():
            wake.clear()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def fixture_batches(fixtures: list[dict[str, Any]], max_items: int = 250, quote_budget: int = 2000):
    batch, quotes = [], 0
    for fixture in fixtures:
        count = len(fixture.get("markets", []))
        if count > 500:
            # 不能截断盘口后仍关闭完整快照中的旧盘，明确暴露协议预算不兼容。
            raise LeyuError("fixture_quote_budget_exceeded")
        if batch and (len(batch) >= max_items or quotes + count > quote_budget):
            yield batch
            batch, quotes = [], 0
        batch.append(fixture)
        quotes += count
    if batch:
        yield batch


def quote_freshness(fixtures: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    observed = now or datetime.now(timezone.utc)
    ages, unknown = [], 0
    for fixture in fixtures:
        for quote in fixture.get("markets", []):
            if quote.get("status") != "active":
                continue
            try:
                stamp = datetime.fromisoformat(str(quote.get("source_updated_at") or "").replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    raise ValueError("timezone missing")
                age = (observed - stamp).total_seconds()
                if age < -5:
                    raise ValueError("upstream clock ahead")
                ages.append(max(0, age))
            except ValueError:
                unknown += 1
    fresh = sum(age <= 60 for age in ages)
    return {"freshness": "fresh" if fresh and fresh == len(ages) and not unknown else "partial" if fresh else "stale" if ages else "unknown",
            "fresh_active_quotes": fresh, "unknown_time_active_quotes": unknown, "known_time_active_quotes": len(ages),
            "newest_quote_age_seconds": round(min(ages), 1) if ages else None,
            "oldest_quote_age_seconds": round(max(ages), 1) if ages else None}


class CollectorService:
    """单个长期工作器；TG、HTTP与WS独立恢复，远端API承担全部持久化。"""

    def __init__(self, api: httpx.AsyncClient, api_url: str, token: str, *, interval: float = INTERVAL,
                 live_odds_refresh_seconds: float = LIVE_ODDS_REFRESH_SECONDS,
                 pregame_near_hours: float = PREGAME_NEAR_HOURS,
                 pregame_near_refresh_seconds: float = PREGAME_NEAR_REFRESH_SECONDS,
                 pregame_far_refresh_seconds: float = PREGAME_FAR_REFRESH_SECONDS,
                 odds_fixture_budget: int = ODDS_FIXTURE_BUDGET,
                 base_config: Mapping[str, Any] | None = None, telegram_factory=TelegramCollector,
                 runtime_factory=LeyuRuntime, realtime_factory=LeyuRealtime):
        if not token:
            raise RuntimeError("COLLECTOR_INTERNAL_TOKEN is required")
        self.api = api
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.interval = max(1, interval)
        self.live_odds_refresh_seconds = max(1, live_odds_refresh_seconds)
        self.pregame_near_hours = max(0, pregame_near_hours)
        self.pregame_near_refresh_seconds = max(1, pregame_near_refresh_seconds)
        self.pregame_far_refresh_seconds = max(self.pregame_near_refresh_seconds, pregame_far_refresh_seconds)
        self._odds_refresh_at: dict[str, float] = {}
        self.odds_fixture_budget = max(1, min(300, odds_fixture_budget))
        self._upload_lock = asyncio.Lock()
        self._poll_alert = ThresholdAlert(1, 0, raise_after=2, recover_after=3)
        self.runtime = runtime_factory(api, self.api_url, base_config)
        self.telegram = telegram_factory({}, api=api, api_url=self.api_url + "/telegram")
        self.resync = asyncio.Event()
        self.status: dict[str, Any] = {
            "leyu": {"state": "starting", "last_error": "", "last_error_code": "", "official_result_confirmation": "unconfigured",
                     "poll_attempts": 0, "poll_successes": 0, "poll_failures": 0,
                     "resync_requests": 0, **self._poll_alert.fields("poll_alert")},
            "telegram": {"state": "starting", "last_error": ""},
        }
        self.telegram.source_health = self.status
        self.realtime = realtime_factory(lambda: self.runtime.config, self.subscription_mids, self.on_push, self.request_resync)
        self.status["websocket"] = self.realtime.stats

    def subscription_mids(self) -> list[str]:
        return [fixture["source_match_id"] for fixture in self.runtime.fixtures.values()
                if fixture.get("phase") in {"scheduled", "live"} and fixture.get("source_match_id")]

    def odds_due_mids(self, fixtures: list[dict[str, Any]], *, now: float | None = None,
                      wall_now: datetime | None = None) -> list[str]:
        monotonic_now = time.monotonic() if now is None else now
        observed = wall_now or datetime.now(timezone.utc)
        queues: dict[str, list[str]] = {"live": [], "near": [], "far": []}
        active = set()
        for fixture in fixtures:
            mid = str(fixture.get("source_match_id") or "")
            if not mid:
                continue
            phase = fixture.get("phase")
            if phase not in {"scheduled", "live"}:
                continue
            active.add(mid)
            if phase == "live":
                budget = self.live_odds_refresh_seconds
                group = "live"
            else:
                try:
                    kickoff = datetime.fromisoformat(str(fixture["kickoff_at"]).replace("Z", "+00:00"))
                    if kickoff.tzinfo is None:
                        continue
                    hours = (kickoff.astimezone(timezone.utc) - observed).total_seconds() / 3600
                except (KeyError, TypeError, ValueError):
                    continue
                budget = (self.pregame_near_refresh_seconds if -1 <= hours <= self.pregame_near_hours
                          else self.pregame_far_refresh_seconds)
                group = "near" if -1 <= hours <= self.pregame_near_hours else "far"
            if monotonic_now - self._odds_refresh_at.get(mid, float("-inf")) >= budget:
                queues[group].append(mid)
        # This is a current schedule projection, not a historical lookup table.
        self._odds_refresh_at = {mid: stamp for mid, stamp in self._odds_refresh_at.items() if mid in active}
        # 每轮限制请求量，但全量赛程不截断。各阶段轮转，远期赛事不会被滚球饿死。
        for queue in queues.values():
            queue.sort(key=lambda mid: self._odds_refresh_at.get(mid, float("-inf")), reverse=True)
        due = []
        limit = getattr(self, "odds_fixture_budget", ODDS_FIXTURE_BUDGET)
        while len(due) < limit and any(queues.values()):
            for group in ("live", "live", "near", "far"):
                if queues[group] and len(due) < limit:
                    due.append(queues[group].pop())
        return due

    def mark_odds_refreshed(self, mids: list[str], *, now: float | None = None) -> None:
        stamp = time.monotonic() if now is None else now
        for mid in mids:
            self._odds_refresh_at[mid] = stamp

    async def request_resync(self) -> None:
        self.status["leyu"]["resync_requests"] += 1
        self.resync.set()

    async def upload(self, fixtures: list[dict[str, Any]], *, odds_mids: list[str] | None = None) -> int:
        accepted = 0
        selected = set(odds_mids) if odds_mids is not None else None
        outgoing = []
        for fixture in fixtures:
            if selected is not None and fixture.get("source_match_id") not in selected:
                outgoing.append({**fixture, "markets": [], "markets_complete": False})
            else:
                outgoing.append(fixture)
        for batch in fixture_batches(outgoing):
            # Poll 与 WS 更新相同记录；在事务边界串行，避免远端行锁互等。
            async with self._upload_lock:
                response = await self.api.post(self.api_url + "/collector/fixtures", json={"items": batch})
            response.raise_for_status()
            document = response.json()
            if not isinstance(document, dict) or not isinstance(document.get("accepted"), int):
                raise LeyuError("invalid_ingestion_receipt")
            accepted += document["accepted"]
        return accepted

    async def on_push(self, events: list[dict[str, Any]]) -> None:
        fixtures, needs_resync = self.runtime.apply_push(events)
        if needs_resync:
            self.resync.set()
        if fixtures:
            accepted = await self.upload(fixtures)
            self.status["leyu"]["last_push_accepted"] = accepted
            self.status["leyu"]["last_push_at"] = datetime.now(timezone.utc).isoformat()

    async def leyu_loop(self, stop: asyncio.Event) -> None:
        failures = 0
        rate_limit_streak = 0
        last_start = 0.0
        while not stop.is_set():
            # 未知推送会请求补同步，但不能制造无间隔全站拉取风暴。
            await wait_for_work(stop, max(0, 2 - (time.monotonic() - last_start)))
            if stop.is_set():
                return
            last_start = time.monotonic()
            poll_at = datetime.now(timezone.utc)
            self.status["leyu"]["poll_attempts"] += 1
            self.resync.clear()
            rate_limited = False
            retry_after = None
            stage = "source"
            problem = False
            try:
                requested_mids: list[str] = []
                def choose_odds(fixtures: list[dict[str, Any]]) -> list[str]:
                    requested_mids[:] = self.odds_due_mids(fixtures)
                    return requested_mids
                fixtures = await self.runtime.collect(odds_mids=choose_odds)
                stage = "ingestion"
                accepted = await self.upload(fixtures, odds_mids=requested_mids)
                self.mark_odds_refreshed([str(f["source_match_id"]) for f in fixtures
                                          if f.get("markets_complete") and f.get("source_match_id")])
                report = self.runtime.last_report
                rate_limited = bool(report.get("rate_limited"))
                retry_after = report.get("retry_after_seconds")
                complete = report.get("complete_market_fixtures", 0)
                freshness = quote_freshness(fixtures)
                expected = report.get("odds_requested_fixtures", len(fixtures))
                first_failure = next(iter(report.get("failed_batches", [])), {})
                reported_error = (report.get("rate_limit_category") or first_failure.get("category")
                                  or self.runtime.config_validation_error or "")
                reported_code = report.get("rate_limit_code") or first_failure.get("code", "")
                reported_group = ("rate_limit" if rate_limited else
                                  failure_group(LeyuError(reported_error)) if reported_error else
                                  "partial_snapshot" if complete != expected else "")
                state = ("degraded" if self.runtime.config_validation_error else
                         "healthy" if not rate_limited and complete == expected and (freshness["freshness"] in {"fresh", "unknown"}) else "partial")
                problem = rate_limited or state != "healthy"
                self.status["leyu"].update(state=state, fixtures=len(fixtures), accepted=accepted,
                                           collection_transport=report.get("collection_transport", "direct"),
                                           complete_market_fixtures=complete,
                                           last_error=reported_error, last_error_code=reported_code,
                                           failure_group=reported_group or ("freshness" if problem else ""),
                                           last_operation=report.get("rate_limit_operation") or first_failure.get("operation", ""),
                                           last_http_status=report.get("rate_limit_http_status") or first_failure.get("http_status"),
                                           failed_batches=len(report.get("failed_batches", [])),
                                           rate_limited=rate_limited,
                                           last_success_at=datetime.now(timezone.utc).isoformat(), config_version=self.runtime.version,
                                           config_validated_at=self.runtime.config_validated_at,
                                           config_validation_error=self.runtime.config_validation_error,
                                           odds_requested_fixtures=expected, **freshness)
                self.status["leyu"]["poll_successes"] += 1
                if not problem:
                    self.status["leyu"]["last_complete_at"] = datetime.now(timezone.utc).isoformat()
                failures = 0
            except Exception as exc:
                failures += 1
                problem = True
                rate_limited = isinstance(exc, RateLimited)
                retry_after = exc.retry_after_seconds if rate_limited else None
                update = {"state": "unavailable", "last_error": category(exc),
                          "last_error_code": exc.code if isinstance(exc, LeyuError) else "",
                          "failure_group": failure_group(exc, stage=stage),
                          "last_operation": exc.operation if isinstance(exc, LeyuError) else stage,
                          "last_http_status": exc.http_status if isinstance(exc, LeyuError) else
                                              exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None,
                          "rate_limited": rate_limited}
                if self.runtime.version > 0:
                    update["config_version"] = self.runtime.version
                if self.runtime.config_validated_at:
                    update["config_validated_at"] = self.runtime.config_validated_at
                update["config_validation_error"] = self.runtime.config_validation_error
                self.status["leyu"].update(update)
                self.status["leyu"]["poll_failures"] += 1
            rate_limit_streak = rate_limit_streak + 1 if rate_limited else 0
            delay = collection_retry_delay(self.interval, failures, rate_limit_streak=rate_limit_streak, retry_after=retry_after)
            self._poll_alert.observe(1 if problem else 0)
            self.status["leyu"].update(last_poll_at=poll_at.isoformat(),
                last_poll_elapsed_ms=round((time.monotonic() - last_start) * 1000, 1),
                consecutive_failures=failures, rate_limit_streak=rate_limit_streak,
                backoff_seconds=delay, retry_at=(datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(),
                **self._poll_alert.fields("poll_alert"))
            report = self.status["leyu"]
            log.log(logging.WARNING if problem else logging.INFO,
                    "Leyu poll state=%s category=%s code=%s operation=%s http_status=%s elapsed_ms=%s retry_seconds=%s config_version=%s last_success_at=%s",
                    report["state"], report["last_error"], report["last_error_code"],
                    report.get("last_operation", ""), report.get("last_http_status"),
                    report["last_poll_elapsed_ms"], delay, report.get("config_version"), report.get("last_success_at"))
            # 上游拒绝与网络失败按退避等待，WS补同步不能绕过失败退避。
            await wait_for_work(stop, delay, self.resync if failures == 0 and not rate_limited else None)

    async def telegram_loop(self, stop: asyncio.Event) -> None:
        failures = 0
        while not stop.is_set():
            try:
                config = await get_config(self.api, self.api_url)
                await self.telegram.apply_config(config)
                async with asyncio.timeout(120):
                    report = await self.telegram.sync_once()
                source_reports = report.get("sources", {}) if isinstance(report, dict) else {}
                source_errors = {
                    chat_id: item.get("error") or item.get("reason") or item.get("state")
                    for chat_id, item in source_reports.items()
                    if isinstance(item, dict) and item.get("state") not in {"caught_up", "catching_up", "backoff"}
                }
                state = report.get("state", "unknown") if isinstance(report, dict) else "unknown"
                self.status["telegram"].update(
                    state=state, last_error=("source_unavailable" if source_errors else ""),
                    source_states={chat_id: item.get("state") for chat_id, item in source_reports.items() if isinstance(item, dict)},
                    source_errors=source_errors, source_count=len(source_reports),
                    degraded=bool(source_errors), media_cleanup=report.get("media_cleanup") if isinstance(report, dict) else None,
                    last_success_at=datetime.now(timezone.utc).isoformat())
                failures = 0
            except Exception as exc:
                failures += 1
                self.status["telegram"].update(state="unavailable", last_error=category(exc))
                log.warning("Telegram source unavailable category=%s", category(exc))
            await wait_for_work(stop, min(self.interval, 10) if failures == 0 else min(60, 2 ** min(failures, 6)))

    async def run(self, stop: asyncio.Event) -> None:
        tasks = []
        try:
            await self.telegram.start_control_server(token=self.token, host=os.environ.get("TELEGRAM_CONTROL_HOST", "0.0.0.0"),
                                                     port=int(os.environ.get("TELEGRAM_CONTROL_PORT", "8092")))
            tasks = [asyncio.create_task(self.leyu_loop(stop), name="leyu-poll"),
                     asyncio.create_task(self.telegram_loop(stop), name="telegram-sync"),
                     asyncio.create_task(self.realtime.run(stop), name="leyu-realtime")]
            stopper = asyncio.create_task(stop.wait(), name="collector-stop")
            tasks.append(stopper)
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is not stopper and not stop.is_set():
                    await task
                    raise RuntimeError("collector component stopped unexpectedly")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.telegram.disconnect()
            self.status["leyu"]["state"] = "stopped"
            self.status["telegram"]["state"] = "stopped"


async def run() -> None:
    configure_logging()
    if not TOKEN:
        raise RuntimeError("COLLECTOR_INTERNAL_TOKEN is required")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)
        async with httpx.AsyncClient(timeout=httpx.Timeout(25, connect=3), limits=limits, trust_env=False,
                                     follow_redirects=False, headers={"Authorization": f"Bearer {TOKEN}"}) as api:
            service = CollectorService(api, API_URL, TOKEN)
            await service.run(stop)
    finally:
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)


if __name__ == "__main__":
    asyncio.run(run())
