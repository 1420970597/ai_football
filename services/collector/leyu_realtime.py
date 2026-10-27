"""有界实时接收与全量重同步契约，不猜测未知推送字段，不写本地历史。"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from datetime import datetime, timezone
import inspect
import json
import random
import time
from typing import Any, Awaitable, Callable, Iterable, Mapping
from urllib.parse import urlencode, urlsplit

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .leyu import LeyuClient
from .leyu_protocol import AUTH_CODES, SUCCESS_CODES, AuthExpired, LeyuError, decode_envelope, decode_payload, objects, parse_quotes, phase_for, score, timestamp

HEARTBEAT_SECONDS = 5.0
HEARTBEAT_DEADLINE_SECONDS = 12.0
SUBSCRIPTION_REFRESH_SECONDS = 15.0
SUBSCRIPTION_BATCH_SIZE = 200
MAX_PUSH_BYTES = 1024 * 1024
PUSH_COMMANDS = frozenset({"C2", "C21", "C101", "C102", "C1021", "C103", "C104", "C105", "C106", "C109", "C110", "C118", "C1301", "C303", "C302"})
ODDS_COMMANDS = frozenset({"C105", "C2", "C21"})
LATENCY_WINDOW_SIZE = 256


def _latency_ms(value: str | None, reference: datetime) -> float | None:
    if not value:
        return None
    try:
        source = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if source.tzinfo is None:
        return None
    lag = (reference - source.astimezone(timezone.utc)).total_seconds() * 1000
    return lag if 0 <= lag <= 7 * 24 * 60 * 60 * 1000 else None


def _percentile(values: deque[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * quantile))], 1)


def subscription_frames(mids: Iterable[str], *, batch_size: int = SUBSCRIPTION_BATCH_SIZE) -> list[dict[str, Any]]:
    """单帧有限但全量不截断，稳定分组键允许后续替换与退订。"""
    values = list(dict.fromkeys(str(value) for value in mids if str(value)))
    if not 1 <= batch_size <= 500:
        raise ValueError("invalid subscription batch")
    return [{"cmd": "C8", "key": f"platform-{start // batch_size}", "list": [{"mid": mid} for mid in values[start:start + batch_size]],
             "cufm": "L", "marketLevel": 0, "esMarketLevel": 0}
            for start in range(0, len(values), batch_size)]


def _push_quotes(row: Mapping[str, Any], mid: str, received_at: datetime) -> list[dict[str, Any]]:
    """已核验 hls2/hls 周期报价形状转换；缺失选项状态不虚构开放状态。"""
    groups = row.get("hls2") or row.get("hls")
    blocks = []
    if isinstance(groups, Mapping):
        for chpid, values in groups.items():
            blocks.extend((str(chpid), block) for block in objects(values))
    elif isinstance(groups, list):
        blocks.extend((str(block.get("chpid") or ""), block) for block in objects(groups))
    quotes = []
    for chpid, block in blocks:
        line = {**block, "ol": [{**option, "os": option.get("os", "unknown")} for option in objects(block.get("ol"))]}
        source_time = block.get("ctsp") or block.get("t") or row.get("time") or row.get("t") or row.get("ctsp")
        normalized = {"mid": mid, "ms": row.get("ms"), "hps": [{
            "chpid": str(block.get("chpid") or chpid), "hpid": block.get("hpid"), "ctsp": source_time, "hl": line,
        }]}
        for quote in parse_quotes(normalized, "leyu:" + mid, received_at):
            option = next((item for item in objects(block.get("ol")) if str(item.get("oid") or "") == quote["selection_id"]), {})
            quote["status_evidence"] = "os" in option and "hs" in block
            quotes.append(quote)
    return quotes


def push_events(message: Any, *, received_at: datetime | None = None) -> list[dict[str, Any]]:
    if not isinstance(message, Mapping):
        raise LeyuError("invalid_push_message")
    code = str(message.get("code") or "")
    if code in AUTH_CODES:
        raise AuthExpired("session_expired", code)
    if code and code not in SUCCESS_CODES:
        decode_envelope(message)
    command = str(message.get("cmd") or "")
    if command not in PUSH_COMMANDS:
        return []
    body = decode_payload(message.get("cd"))
    if not isinstance(body, (list, Mapping)):
        return [{"command": command, "requires_resync": True, "fixture_ids": []}]
    rows = body if isinstance(body, list) else [body]
    result = []
    received = received_at or datetime.now(timezone.utc)
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        mid = str(row.get("mid") or message.get("mid") or "")
        if not mid:
            # 没有已核验的比赛标识时只能请求重同步，不能把未知结构当空快照。
            result.append({"command": command, "requires_resync": True, "fixture_ids": []})
            continue
        source_time = timestamp(row.get("ctsp") or row.get("time") or row.get("t") or row.get("ts") or message.get("ctsp") or message.get("ts"))
        event = {"command": command, "fixture_ids": [mid], "fixture_id": f"leyu:{mid}",
                       "source_updated_at": source_time, "source_time_quality": "known" if source_time else "unknown",
                       "received_at": timestamp(received), "payload": dict(row),
                       "requires_resync": command not in ODDS_COMMANDS | {"C101", "C102", "C1021", "C103"}}
        if command in ODDS_COMMANDS:
            event["markets"] = _push_quotes(row, mid, received)
            if not event["markets"]:
                event["requires_resync"] = True
        else:
            patch = {}
            if "msc" in row:
                for prefix, period in (("", "S1"), ("half_", "S2")):
                    home, away = score(row["msc"], period)
                    if home is not None:
                        patch[prefix + "home_score"], patch[prefix + "away_score"] = home, away
            if str(row.get("ms", "")) in {"0", "1", "3"} or str(row.get("mmp", "")) == "999":
                patch["phase"] = phase_for(row)
            event["fixture_patch"] = patch
            if not patch:
                event["requires_resync"] = True
        result.append(event)
    return result


async def _value(value):
    return await value if inspect.isawaitable(value) else value


class LeyuRealtime:
    def __init__(self, config_provider: Callable[[], Any], mids_provider: Callable[[], Any],
                 on_batch: Callable[[list[dict[str, Any]]], Awaitable[None]],
                 on_resync: Callable[[], Awaitable[None]], *, connector=connect, queue_size: int = 128):
        self.config_provider = config_provider
        self.mids_provider = mids_provider
        self.on_batch = on_batch
        self.on_resync = on_resync
        self.connector = connector
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max(1, min(queue_size, 256)))
        self._source_lags: deque[float] = deque(maxlen=LATENCY_WINDOW_SIZE)
        self._queue_waits: deque[float] = deque(maxlen=LATENCY_WINDOW_SIZE)
        self._delivery_ages: deque[float] = deque(maxlen=LATENCY_WINDOW_SIZE)
        self.stats: dict[str, Any] = {
            "state": "stopped", "reconnects": 0, "received": 0, "events_received": 0,
            "events_delivered": 0, "batches_delivered": 0, "dropped": 0,
            "delivery_failures": 0, "queue_depth": 0, "queue_high_water": 0,
            "subscription_requests_sent": 0,
            "source_lag_ms_p50": None, "source_lag_ms_p95": None,
            "source_lag_samples": 0, "queue_wait_ms_p50": None,
            "queue_wait_ms_p95": None, "queue_wait_samples": 0,
            "delivery_age_ms_p50": None, "delivery_age_ms_p95": None,
            "delivery_age_samples": 0,
            "last_event_at": None, "last_delivery_at": None, "last_error": "",
        }
        self._gap = False

    def _record_event(self, event: Mapping[str, Any]) -> None:
        received_at = event.get("received_at")
        self.stats["last_event_at"] = received_at
        try:
            received = datetime.fromisoformat(str(received_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            received = None
        lag = _latency_ms(event.get("source_updated_at"), received) if received and received.tzinfo else None
        if lag is not None:
            self._source_lags.append(lag)
            self.stats.update(
                source_lag_ms_p50=_percentile(self._source_lags, 0.5),
                source_lag_ms_p95=_percentile(self._source_lags, 0.95),
                source_lag_samples=len(self._source_lags),
            )

    def _record_queue_wait(self, event: Mapping[str, Any]) -> None:
        try:
            received = datetime.fromisoformat(str(event.get("received_at")).replace("Z", "+00:00"))
            if received.tzinfo is None:
                return
            wait_ms = max(0.0, (datetime.now(timezone.utc) - received.astimezone(timezone.utc)).total_seconds() * 1000)
        except (TypeError, ValueError):
            return
        self._queue_waits.append(wait_ms)
        self.stats.update(
            queue_wait_ms_p50=_percentile(self._queue_waits, 0.5),
            queue_wait_ms_p95=_percentile(self._queue_waits, 0.95),
            queue_wait_samples=len(self._queue_waits),
        )

    def _enqueue_event(self, event: dict[str, Any]) -> bool:
        self._record_event(event)
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.stats["dropped"] += 1
            self.stats["queue_depth"] = self.queue.qsize()
            self._gap = True
            return False
        self.stats["events_received"] += 1
        self.stats["queue_depth"] = self.queue.qsize()
        self.stats["queue_high_water"] = max(self.stats["queue_high_water"], self.queue.qsize())
        return True

    async def _subscriptions(self, socket, previous: dict[str, dict]) -> dict[str, dict]:
        mids = await _value(self.mids_provider())
        if not isinstance(mids, (list, tuple, set)):
            raise LeyuError("subscription_source_unavailable")
        frames = subscription_frames(mids)
        current = {frame["key"]: frame for frame in frames}
        for key, frame in previous.items():
            if key not in current or current[key] != frame:
                await socket.send(json.dumps({**frame, "cclose": "1"}, separators=(",", ":")))
        for key, frame in current.items():
            if previous.get(key) != frame:
                await socket.send(json.dumps(frame, separators=(",", ":")))
        requested = sum(len(frame["list"]) for frame in frames)
        self.stats["subscription_requests_sent"] = requested
        self.stats["subscribed_fixtures"] = requested
        return current

    async def _consume(self, stop: asyncio.Event) -> None:
        async def deliver(callback, *args) -> bool:
            failures = 0
            while not stop.is_set():
                try:
                    await asyncio.wait_for(callback(*args), timeout=30)
                    if self.stats["last_error"] == "push_delivery_failure":
                        self.stats["last_error"] = ""
                    return True
                except Exception:
                    # 写入失败时保留当前批次重试，不能使消费者静默退出且网络线程仍报健康。
                    self.stats["last_error"] = "push_delivery_failure"
                    self.stats["delivery_failures"] = self.stats.get("delivery_failures", 0) + 1
                    failures += 1
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=min(5, 0.1 * 2 ** min(failures, 6)))
                    except asyncio.TimeoutError:
                        pass
            return False

        while not stop.is_set() or not self.queue.empty():
            if self._gap:
                self._gap = False
                if not await deliver(self.on_resync):
                    return
            try:
                event = await asyncio.wait_for(self.queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            self._record_queue_wait(event)
            combined: OrderedDict[tuple, dict[str, Any]] = OrderedDict()
            items = [event]
            while not self.queue.empty() and len(items) < 100:
                items.append(self.queue.get_nowait())
            self.stats["queue_depth"] = self.queue.qsize()
            for item in items:
                if item is not event:
                    self._record_queue_wait(item)
                raw = item.get("payload", {})
                # hls2 为多选项快照；不同结构不可仅按比赛覆盖而丢独立盘口变动。
                identity = json.dumps(raw, sort_keys=True, separators=(",", ":"))
                key = (item.get("fixture_id"), item.get("command"), identity)
                combined[key] = item
            try:
                if any(item.get("requires_resync") for item in combined.values()):
                    if not await deliver(self.on_resync):
                        return
                batch = list(combined.values())
                if not await deliver(self.on_batch, batch):
                    return
                self.stats["events_delivered"] += len(batch)
                self.stats["batches_delivered"] += 1
                delivered_at = datetime.now(timezone.utc)
                self.stats["last_delivery_at"] = delivered_at.isoformat()
                for item in batch:
                    age = _latency_ms(item.get("source_updated_at"), delivered_at)
                    if age is not None:
                        self._delivery_ages.append(age)
                self.stats.update(
                    delivery_age_ms_p50=_percentile(self._delivery_ages, 0.5),
                    delivery_age_ms_p95=_percentile(self._delivery_ages, 0.95),
                    delivery_age_samples=len(self._delivery_ages),
                )
            finally:
                for _ in items:
                    self.queue.task_done()
                self.stats["queue_depth"] = self.queue.qsize()

    async def run(self, stop: asyncio.Event) -> None:
        failures = 0
        consumer = asyncio.create_task(self._consume(stop))
        try:
            while not stop.is_set():
                try:
                    config = await _value(self.config_provider())
                    client = LeyuClient.from_config(config)
                    host = urlsplit(client.base_url)
                    url = "wss://" + host.netloc + "/yewuws2/push?" + urlencode({"requestId": client.request_id})
                    self.stats["state"] = "connecting"
                    async with self.connector(url, origin=client.origin, additional_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
                                              user_agent_header=client.user_agent, proxy=None, open_timeout=5,
                                              ping_interval=5, ping_timeout=8, close_timeout=3,
                                              max_size=MAX_PUSH_BYTES, max_queue=4) as socket:
                        await self.on_resync()
                        previous = await self._subscriptions(socket, {})
                        now = time.monotonic()
                        last_received = now
                        heartbeat_at = now
                        subscriptions_at = now + SUBSCRIPTION_REFRESH_SECONDS
                        self.stats.update(state="connected", last_error="")
                        failures = 0
                        while not stop.is_set():
                            if consumer.done():
                                await consumer
                            now = time.monotonic()
                            if now >= heartbeat_at:
                                await socket.send(json.dumps({"cmd": "C0", "requestId": client.request_id}, separators=(",", ":")))
                                heartbeat_at = now + HEARTBEAT_SECONDS
                            if now >= subscriptions_at:
                                latest = LeyuClient.from_config(await _value(self.config_provider()))
                                if (latest.base_url, latest.origin, latest.request_id, latest.cookie, latest.token, latest.user_agent) != (
                                        client.base_url, client.origin, client.request_id, client.cookie, client.token, client.user_agent):
                                    raise LeyuError("configuration_changed")
                                previous = await self._subscriptions(socket, previous)
                                subscriptions_at = now + SUBSCRIPTION_REFRESH_SECONDS
                            if now - last_received > HEARTBEAT_DEADLINE_SECONDS:
                                raise LeyuError("websocket_heartbeat_timeout")
                            try:
                                raw = await asyncio.wait_for(socket.recv(), timeout=0.5)
                            except asyncio.TimeoutError:
                                continue
                            last_received = time.monotonic()
                            self.stats["received"] += 1
                            try:
                                message = json.loads(raw)
                                events = push_events(message)
                            except (ValueError, UnicodeError, LeyuError) as exc:
                                if isinstance(exc, AuthExpired):
                                    raise
                                self.stats["last_error"] = "invalid_push_message"
                                self._gap = True
                                continue
                            for event in events:
                                self._enqueue_event(event)
                except (LeyuError, OSError, ConnectionClosed, InvalidStatus, TimeoutError) as exc:
                    if isinstance(exc, LeyuError):
                        category = exc.category
                    elif isinstance(exc, InvalidStatus):
                        # HTTP handshake refusals (notably 403) are an upstream
                        # condition. Keep the collector alive and reconnect with
                        # backoff; never expose response headers/body in status.
                        status = getattr(getattr(exc, "response", None), "status_code", None)
                        category = f"websocket_http_{status}" if isinstance(status, int) else "websocket_handshake_rejected"
                    else:
                        category = type(exc).__name__
                    self.stats.update(state="backoff", last_error=category)
                    self.stats["reconnects"] += 1
                    failures += 1
                    wait = min(60, 2 ** min(failures, 5)) + random.random()
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=wait)
                    except asyncio.TimeoutError:
                        pass
        finally:
            self.stats["state"] = "stopped"
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
