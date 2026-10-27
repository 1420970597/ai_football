from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import secrets
import socket
import time
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit

import httpx

from .leyu_protocol import (
    AuthExpired, LeyuError, MAX_RESPONSE_BYTES, RateLimited, SUCCESS_CODES,
    decode_envelope, decode_payload, parse_fixtures, parse_quotes, timestamp,
)

DEFAULT_FIXTURE_PATH = "/yewu11/v2/m/getOriginalDataPB"
DEFAULT_ODDS_PATH = "/yewu11/v1/w/structureMatchBaseInfoByMidsPB"
DEFAULT_MATCH_ODDS_PATH = "/yewu11/v1/w/getMatchBaseInfoByOddsPB"
DEFAULT_BATCH_SIZE = 12
DEFAULT_CONCURRENCY = 1
DEFAULT_BATCH_INTERVAL = 1.0
MAX_RETRY_AFTER_SECONDS = 900
DEFAULT_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")


def safe_base_url(value: str, *, allow_http: bool = False) -> str:
    text = value.strip()
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise LeyuError("invalid_base_url") from None
    schemes = {"https", "http"} if allow_http else {"https"}
    if (parts.scheme not in schemes or not parts.hostname or parts.username or parts.password
            or any(ord(c) <= 32 or ord(c) == 127 for c in text) or "\\" in text
            or (port is not None and not 1 <= port <= 65535)
            or parts.query or parts.fragment or parts.path not in ("", "/")):
        raise LeyuError("invalid_base_url")
    return f"{parts.scheme}://{parts.netloc}".rstrip("/")


def _decode_payload(value: Any) -> Any:
    return decode_payload(value)


def _body(response: httpx.Response) -> Any:
    try:
        return decode_envelope(response.json())
    except (json.JSONDecodeError, UnicodeError):
        raise LeyuError("invalid_json_response") from None


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> int | None:
    """Read HTTP Retry-After without retaining its raw content or unbounded waits."""
    if not value or len(value) > 128:
        return None
    try:
        delay = float(value)
        if not math.isfinite(delay) or delay < 0:
            return None
    except ValueError:
        try:
            stamp = parsedate_to_datetime(value)
            if stamp.tzinfo is None:
                return None
            delay = max(0, (stamp - (now or datetime.now(timezone.utc))).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
    return min(MAX_RETRY_AFTER_SECONDS, math.ceil(delay))


def _dns_failure(error: BaseException) -> bool:
    for _ in range(8):
        if isinstance(error, socket.gaierror):
            return True
        cause = error.__cause__ or error.__context__
        if cause is None or cause is error:
            return False
        error = cause
    return False


class LeyuClient:
    """授权采集客户端；显式 HTTPS 中转与登录代理独立，禁止继承环境代理。"""

    def __init__(
        self, base_url: str, token: str = "", request_id: str = "", *,
        origin: str = "", cookie: str = "", user_agent: str = DEFAULT_USER_AGENT,
        cuid: str = "", check_id_base: str = "", batch_size: int = DEFAULT_BATCH_SIZE,
        concurrency: int = DEFAULT_CONCURRENCY, batch_interval: float = DEFAULT_BATCH_INTERVAL,
        collection_relay: str = "",
        transport: httpx.AsyncBaseTransport | None = None, allow_http: bool = False,
    ):
        self.base_url = safe_base_url(base_url, allow_http=allow_http)
        self.collection_relay = safe_base_url(collection_relay) if collection_relay else ""
        self.origin = safe_base_url(origin, allow_http=allow_http) if origin else self.base_url
        # requestId 是授权会话，不能用随机追踪标识回退。
        self.request_id = request_id.strip()
        if not self.request_id:
            raise LeyuError("authorized_session_missing")
        self.token = token.strip()
        self.cookie = cookie.strip()
        self.user_agent = user_agent.strip()
        self.cuid = cuid.strip() or str(int(time.time() * 1000)) + "1"
        self.check_id_base = check_id_base.strip() or secrets.token_hex(16)
        self.batch_size = max(1, min(50, batch_size))
        self.concurrency = max(1, min(4, concurrency))
        self.batch_interval = max(0, min(5.0, batch_interval))
        self.transport = transport
        self.last_report: dict[str, Any] = {}

    @classmethod
    def validate_collection_budget(cls, config: Mapping[str, Any]) -> dict[str, Any]:
        def text(name: str, default: str = "") -> str:
            return str(config.get(name) or default).strip()
        try:
            batch_size = int(text("leyu_batch_size", str(DEFAULT_BATCH_SIZE)))
            concurrency = int(text("leyu_batch_concurrency", str(DEFAULT_CONCURRENCY)))
            interval = float(text("leyu_batch_interval_seconds", str(DEFAULT_BATCH_INTERVAL)))
        except ValueError:
            raise LeyuError("invalid_collection_budget") from None
        if not (1 <= batch_size <= 50 and 1 <= concurrency <= 4 and math.isfinite(interval) and 0 <= interval <= 5):
            raise LeyuError("invalid_collection_budget")
        return {"batch_size": batch_size, "concurrency": concurrency, "batch_interval": interval}

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, Any]:
        def text(name: str, default: str = "") -> str:
            return str(config.get(name) or default).strip()
        budget = cls.validate_collection_budget(config)
        base_url = safe_base_url(text("leyu_api_base"))
        request_id = text("leyu_request_id")
        if not request_id:
            raise LeyuError("authorized_session_missing")
        origin = safe_base_url(text("leyu_origin")) if text("leyu_origin") else base_url
        relay = safe_base_url(text("leyu_collection_relay")) if text("leyu_collection_relay") else ""
        return {
            "base_url": base_url, "request_id": request_id, "origin": origin,
            "collection_relay": relay,
            "token": text("leyu_business_cookie_token"), "cookie": text("leyu_cookie"),
            "user_agent": text("leyu_user_agent", DEFAULT_USER_AGENT), "cuid": text("leyu_cuid"),
            "check_id_base": text("leyu_check_id_base"), **budget,
        }

    @classmethod
    def from_config(cls, config: Mapping[str, Any], **kwargs) -> "LeyuClient":
        values = cls.validate_config(config)
        return cls(
            # App token 只用于 App 续期；业务 Cookie 必须有独立明确配置。
            values["base_url"], values["token"], values["request_id"], origin=values["origin"],
            cookie=values["cookie"], user_agent=values["user_agent"], cuid=values["cuid"],
            check_id_base=values["check_id_base"], batch_size=values["batch_size"],
            concurrency=values["concurrency"], batch_interval=values["batch_interval"],
            collection_relay=values["collection_relay"], **kwargs,
        )

    def collection_url(self, path: str) -> str:
        if path not in {DEFAULT_FIXTURE_PATH, DEFAULT_ODDS_PATH, DEFAULT_MATCH_ODDS_PATH}:
            raise LeyuError("unsupported_collection_path")
        if self.collection_relay:
            return self.collection_relay + "/" + urlsplit(self.base_url).netloc + path
        return self.base_url + path

    def headers(self) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/plain, */*", "accept-language": "zh-CN,zh;q=0.9",
            "lang": "zh", "request-code": '{"panda-bss-source":"2"}',
            "requestId": self.request_id,
            "checkId": f"pc-{self.check_id_base}-{self.cuid}-{int(time.time() * 1000)}",
            "origin": self.origin, "referer": self.origin + "/", "user-agent": self.user_agent,
            "sec-fetch-site": "cross-site", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty",
        }
        cookie = self.cookie
        if self.token and "x-api-token=" not in cookie.lower():
            cookie = (cookie + "; " if cookie else "") + "X-API-TOKEN=" + self.token
        if cookie:
            headers["cookie"] = cookie
        if self.collection_relay:
            headers.update({"cache-control": "no-store", "pragma": "no-cache"})
        return headers

    async def _request(self, client: httpx.AsyncClient, method: str, path: str, **kwargs) -> Any:
        # Never include URL/body/upstream text in raised diagnostics: some include account tokens.
        operation = {DEFAULT_FIXTURE_PATH: "fixture_snapshot", DEFAULT_ODDS_PATH: "odds_batch",
                     DEFAULT_MATCH_ODDS_PATH: "odds_single"}.get(path, "")
        status = None
        try:
            async with client.stream(
                method, self.collection_url(path), params={"t": int(time.time() * 1000)},
                headers=self.headers(), **kwargs,
            ) as response:
                status = response.status_code
                if response.status_code == 429:
                    raise RateLimited("http_rate_limited", "429",
                                      retry_after_seconds=retry_after_seconds(response.headers.get("Retry-After")))
                if response.status_code in {401, 403}:
                    # HTTP 403 alone does not prove token expiration.
                    raise LeyuError("http_access_denied", str(response.status_code))
                if response.status_code >= 300:
                    raise LeyuError("http_upstream_error", str(response.status_code))
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise LeyuError("response_too_large")
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeError):
                    raise LeyuError("invalid_json_response") from None
                try:
                    return decode_envelope(payload)
                except RateLimited as exc:
                    exc.retry_after_seconds = retry_after_seconds(response.headers.get("Retry-After"))
                    raise
        except LeyuError as exc:
            exc.operation, exc.http_status = operation, status
            raise
        except httpx.TimeoutException:
            raise LeyuError("upstream_timeout", operation=operation, http_status=status) from None
        except httpx.ConnectError as exc:
            raise LeyuError("upstream_domain_unresolved" if _dns_failure(exc) else "upstream_transport_error",
                            operation=operation, http_status=status) from None
        except httpx.HTTPError:
            raise LeyuError("upstream_transport_error", operation=operation, http_status=status) from None

    async def fetch(self, *, odds_mids: Iterable[str] | Callable[[list[dict[str, Any]]], Iterable[str]] | None = None) -> list[dict[str, Any]]:
        try:
            async with asyncio.timeout(120):
                return await self._fetch(odds_mids=odds_mids)
        except TimeoutError:
            raise LeyuError("collection_deadline_exceeded") from None

    @staticmethod
    def validate_match_id(value: str) -> str:
        if (not isinstance(value, str) or not 1 <= len(value) <= 96 or not value.isascii()
                or not all(char.isalnum() or char in "_-" for char in value)):
            raise LeyuError("invalid_source_match_id")
        return value

    @staticmethod
    def _apply_market_row(fixture: dict[str, Any], row: Mapping[str, Any], captured: datetime) -> None:
        parsed = parse_fixtures([row], received_at=captured)
        if parsed:
            detail = parsed[0]
            # The schedule owns competition identity; detail supplies state/score.
            for key in ("phase", "match_state", "home_score", "away_score", "half_home_score",
                        "half_away_score", "clock_minute", "clock_raw", "source_updated_at",
                        "source_time_quality", "received_at"):
                fixture[key] = detail[key]
        fixture["markets"] = parse_quotes(row, fixture["id"], captured)
        fixture["markets_complete"] = isinstance(row.get("hpsData"), (list, Mapping))
        if not fixture["markets_complete"]:
            fixture["collection_error"] = "market_container_missing"
        else:
            fixture.pop("collection_error", None)

    async def fetch_match(self, source_match_id: str, *, fixture: Mapping[str, Any]) -> dict[str, Any]:
        """One existing, bound match detail request; never fetch the station or renew login."""
        mid = self.validate_match_id(source_match_id)
        if str(fixture.get("source_match_id") or "") != mid or not fixture.get("id"):
            raise LeyuError("fixture_identity_mismatch")
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(6, connect=3, pool=1),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            transport=self.transport, trust_env=False, follow_redirects=False,
        ) as client:
            details = await self._request(client, "POST", DEFAULT_MATCH_ODDS_PATH, json={
                "cuid": self.cuid, "cos": 0, "orpt": 0, "euid": "3020101",
                "mid": mid, "mcid": 0, "newUser": 0,
            })
        rows = details.get("data") if isinstance(details, Mapping) and "data" in details else details
        if isinstance(rows, Mapping):
            rows = [rows]
        if not isinstance(rows, list):
            raise LeyuError("invalid_single_match_odds_rows")
        matches = [row for row in rows if isinstance(row, Mapping) and str(row.get("mid")) == mid]
        if len(matches) != 1:
            raise LeyuError("fixture_detail_missing" if not matches else "ambiguous_fixture_detail")
        result = deepcopy(dict(fixture))
        self._apply_market_row(result, matches[0], datetime.now(timezone.utc))
        if not result["markets_complete"]:
            raise LeyuError("market_container_missing")
        return result

    async def _fetch(self, *, odds_mids: Iterable[str] | Callable[[list[dict[str, Any]]], Iterable[str]] | None = None) -> list[dict[str, Any]]:
        """完整赛程 + 有预算批次补盘；失败批次不会被当作完整空盘口快照。"""
        limits = httpx.Limits(max_connections=self.concurrency + 1, max_keepalive_connections=self.concurrency + 1)
        timeout = httpx.Timeout(20, connect=5, pool=5)
        async with httpx.AsyncClient(
            timeout=timeout, limits=limits, transport=self.transport,
            trust_env=False, follow_redirects=False,
        ) as client:
            received = datetime.now(timezone.utc)
            payload = await self._request(client, "GET", DEFAULT_FIXTURE_PATH)
            fixtures = parse_fixtures(payload, received_at=received)
            if not fixtures:
                # 原始空赛程可以为空；无法解析的非空结果不能显示健康。
                raw_rows = payload.get("matchsList", []) if isinstance(payload, Mapping) else payload
                if raw_rows:
                    raise LeyuError("no_valid_fixtures")
            by_id = {fixture["source_match_id"]: fixture for fixture in fixtures}
            for fixture in fixtures:
                fixture["markets"] = []
                fixture["markets_complete"] = False
            mids = list(by_id)
            requested = odds_mids(fixtures) if callable(odds_mids) else odds_mids
            selected = set(mids) if requested is None else {str(mid) for mid in requested} & set(by_id)
            selected_mids = [mid for mid in mids if mid in selected]
            failures: list[dict[str, Any]] = []
            semaphore = asyncio.Semaphore(self.concurrency)
            rate_limit = asyncio.Lock()
            next_request = 0.0
            single_match_fallbacks = 0
            limited: RateLimited | None = None
            fallback_budget = self.batch_size
            fallback_attempts = 0

            async def pace_request() -> None:
                nonlocal next_request
                async with rate_limit:
                    wait = next_request - time.monotonic()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    next_request = time.monotonic() + self.batch_interval

            def apply_market_row(fixture: dict[str, Any], row: Mapping[str, Any], captured: datetime) -> None:
                self._apply_market_row(fixture, row, captured)

            async def fetch_single_match(mid: str) -> Mapping[str, Any] | None:
                nonlocal single_match_fallbacks, fallback_attempts
                if limited is not None:
                    raise limited
                if fallback_attempts >= fallback_budget:
                    raise LeyuError("single_match_fallback_budget_exhausted")
                fallback_attempts += 1
                body = {"cuid": self.cuid, "cos": 0, "orpt": 0, "euid": "3020101",
                        "mid": mid, "mcid": 0, "newUser": 0}
                await pace_request()
                if limited is not None:
                    raise limited
                details = await self._request(client, "POST", DEFAULT_MATCH_ODDS_PATH, json=body)
                single_match_fallbacks += 1
                rows = details.get("data") if isinstance(details, Mapping) and "data" in details else details
                if isinstance(rows, Mapping):
                    rows = [rows]
                if not isinstance(rows, list):
                    raise LeyuError("invalid_single_match_odds_rows")
                return next((row for row in rows if isinstance(row, Mapping) and str(row.get("mid")) == mid), None)

            async def fetch_batch(batch: list[str]) -> None:
                nonlocal limited
                async with semaphore:
                    try:
                        if limited is not None:
                            raise limited
                        await pace_request()
                        if limited is not None:
                            raise limited
                        body = {
                            "mids": ",".join(batch), "cuid": self.cuid,
                            "cos": 0, "orpt": 0, "euid": "3020101",
                        }
                        details = await self._request(client, "POST", DEFAULT_ODDS_PATH, json=body)
                        if isinstance(details, Mapping):
                            rows = details.get("data")
                        else:
                            rows = details
                        if not isinstance(rows, list):
                            raise LeyuError("invalid_odds_rows")
                        detail_by_id = {str(row.get("mid")): row for row in rows if isinstance(row, Mapping)}
                        captured = datetime.now(timezone.utc)
                        for mid in batch:
                            fixture = by_id[mid]
                            row = detail_by_id.get(mid)
                            if row is None:
                                try:
                                    row = await fetch_single_match(mid)
                                except (AuthExpired, RateLimited):
                                    raise
                                except LeyuError as exc:
                                    failures.append({"size": 1, "fallback": True,
                                                     "category": exc.category, "code": exc.code,
                                                     "operation": exc.operation or "odds_single", "http_status": exc.http_status})
                                    fixture["markets_complete"] = False
                                    fixture["collection_error"] = exc.category
                                    continue
                            if row is None:
                                fixture["markets_complete"] = False
                                fixture["collection_error"] = "fixture_detail_missing"
                                continue
                            apply_market_row(fixture, row, captured)
                    except AuthExpired:
                        raise
                    except RateLimited as exc:
                        limited = exc
                        failures.append({"size": len(batch), "category": exc.category, "code": exc.code,
                                         "operation": exc.operation or "odds_batch", "http_status": exc.http_status})
                        for mid in batch:
                            if not by_id[mid]["markets_complete"]:
                                by_id[mid]["collection_error"] = exc.category
                    except LeyuError as exc:
                        failures.append({"size": len(batch), "category": exc.category, "code": exc.code,
                                         "operation": exc.operation or "odds_batch", "http_status": exc.http_status})
                        for mid in batch:
                            by_id[mid]["markets_complete"] = False
                            by_id[mid]["collection_error"] = exc.category

            tasks = [asyncio.create_task(fetch_batch(selected_mids[start:start + self.batch_size]))
                     for start in range(0, len(selected_mids), self.batch_size)]
            try:
                if tasks:
                    await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            self.last_report = {
                "collection_transport": "https_relay" if self.collection_relay else "direct",
                "fixtures": len(fixtures),
                "odds_requested_fixtures": len(selected_mids),
                "complete_market_fixtures": sum(bool(f.get("markets_complete")) for f in fixtures),
                "single_match_fallbacks": single_match_fallbacks,
                "single_match_fallback_attempts": fallback_attempts,
                "rate_limited": limited is not None,
                "rate_limit_category": limited.category if limited is not None else "",
                "rate_limit_code": limited.code if limited is not None else "",
                "rate_limit_operation": limited.operation if limited is not None else "",
                "rate_limit_http_status": limited.http_status if limited is not None else None,
                "retry_after_seconds": limited.retry_after_seconds if limited is not None else None,
                "failed_batches": failures,
                "received_at": timestamp(datetime.now(timezone.utc)),
            }
            return fixtures
