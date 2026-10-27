from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import os
import signal
import time
from typing import Any, Mapping

import httpx

from .leyu_protocol import LeyuError
from .leyu_realtime import LeyuRealtime
from .leyu_runtime import LeyuRuntime
from .telegram import TelegramCollector

log = logging.getLogger("platform.collector")
API_URL = os.environ.get("PLATFORM_API", "http://api:8080/internal").rstrip("/")
TOKEN = os.environ.get("COLLECTOR_INTERNAL_TOKEN", "")
INTERVAL = max(10, min(300, int(os.environ.get("COLLECTOR_INTERVAL_SECONDS", "20"))))


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


def fixture_batches(fixtures: list[dict[str, Any]], max_items: int = 50, quote_budget: int = 2000):
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
                 base_config: Mapping[str, Any] | None = None, telegram_factory=TelegramCollector,
                 runtime_factory=LeyuRuntime, realtime_factory=LeyuRealtime):
        if not token:
            raise RuntimeError("COLLECTOR_INTERNAL_TOKEN is required")
        self.api = api
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.interval = max(1, interval)
        self.runtime = runtime_factory(api, self.api_url, base_config)
        self.telegram = telegram_factory({}, api=api, api_url=self.api_url + "/telegram")
        self.resync = asyncio.Event()
        self.status: dict[str, Any] = {
            "leyu": {"state": "starting", "last_error": "", "official_result_confirmation": "unconfigured"},
            "telegram": {"state": "starting", "last_error": ""},
        }
        self.telegram.source_health = self.status
        self.realtime = realtime_factory(lambda: self.runtime.config, self.subscription_mids, self.on_push, self.request_resync)
        self.status["websocket"] = self.realtime.stats

    def subscription_mids(self) -> list[str]:
        return [fixture["source_match_id"] for fixture in self.runtime.fixtures.values()
                if fixture.get("phase") in {"scheduled", "live"} and fixture.get("source_match_id")]

    async def request_resync(self) -> None:
        self.resync.set()

    async def upload(self, fixtures: list[dict[str, Any]]) -> int:
        accepted = 0
        for batch in fixture_batches(fixtures):
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
        last_start = 0.0
        while not stop.is_set():
            # 未知推送会请求补同步，但不能制造无间隔全站拉取风暴。
            await wait_for_work(stop, max(0, 2 - (time.monotonic() - last_start)))
            if stop.is_set():
                return
            last_start = time.monotonic()
            self.resync.clear()
            try:
                fixtures = await self.runtime.collect()
                accepted = await self.upload(fixtures)
                report = self.runtime.last_report
                complete = report.get("complete_market_fixtures", 0)
                freshness = quote_freshness(fixtures)
                state = "healthy" if complete == len(fixtures) and freshness["freshness"] == "fresh" else "partial"
                self.status["leyu"].update(state=state, fixtures=len(fixtures), accepted=accepted,
                                           complete_market_fixtures=complete, last_error="",
                                           last_success_at=datetime.now(timezone.utc).isoformat(), config_version=self.runtime.version, **freshness)
                log.info("Leyu source state=%s fixtures=%d accepted=%d", state, len(fixtures), accepted)
                failures = 0
            except Exception as exc:
                failures += 1
                update = {"state": "unavailable", "last_error": category(exc)}
                if self.runtime.version > 0:
                    update["config_version"] = self.runtime.version
                self.status["leyu"].update(update)
                log.warning("Leyu source unavailable category=%s", category(exc))
            delay = self.interval if failures == 0 else min(60, 2 ** min(failures, 6))
            # 上游拒绝与网络失败按退避等待，WS补同步不能绕过失败退避。
            await wait_for_work(stop, delay, self.resync if failures == 0 else None)

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
