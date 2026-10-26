from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import secrets
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from .leyu_protocol import (
    AuthExpired, LeyuError, MAX_RESPONSE_BYTES, RateLimited, SUCCESS_CODES,
    decode_envelope, decode_payload, parse_fixtures, parse_quotes, timestamp,
)

DEFAULT_FIXTURE_PATH = "/yewu11/v2/m/getOriginalDataPB"
DEFAULT_ODDS_PATH = "/yewu11/v1/w/structureMatchBaseInfoByMidsPB"
DEFAULT_BATCH_SIZE = 12
DEFAULT_CONCURRENCY = 2
DEFAULT_BATCH_INTERVAL = 0.2


def safe_base_url(value: str, *, allow_http: bool = False) -> str:
    parts = urlsplit(value.strip())
    schemes = {"https", "http"} if allow_http else {"https"}
    if (parts.scheme not in schemes or not parts.netloc or parts.username or parts.password
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


class LeyuClient:
    """授权业务会话客户端。代理只属于登录适配器，本客户端禁止继承环境代理。"""

    def __init__(
        self, base_url: str, token: str = "", request_id: str = "", *,
        origin: str = "", cookie: str = "", user_agent: str = "Mozilla/5.0",
        cuid: str = "", check_id_base: str = "", batch_size: int = DEFAULT_BATCH_SIZE,
        concurrency: int = DEFAULT_CONCURRENCY, batch_interval: float = DEFAULT_BATCH_INTERVAL,
        transport: httpx.AsyncBaseTransport | None = None, allow_http: bool = False,
    ):
        self.base_url = safe_base_url(base_url, allow_http=allow_http)
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
    def from_config(cls, config: Mapping[str, Any], **kwargs) -> "LeyuClient":
        def text(name: str, default: str = "") -> str:
            return str(config.get(name) or default).strip()
        try:
            batch_size = int(text("leyu_batch_size", str(DEFAULT_BATCH_SIZE)))
            concurrency = int(text("leyu_batch_concurrency", str(DEFAULT_CONCURRENCY)))
            interval = float(text("leyu_batch_interval_seconds", str(DEFAULT_BATCH_INTERVAL)))
        except ValueError:
            raise LeyuError("invalid_collection_budget") from None
        return cls(
            # App token 只用于 App 续期；业务 Cookie 必须有独立明确配置。
            text("leyu_api_base"), text("leyu_business_cookie_token"), text("leyu_request_id"),
            origin=text("leyu_origin"), cookie=text("leyu_cookie"),
            user_agent=text("leyu_user_agent", "Mozilla/5.0"), cuid=text("leyu_cuid"),
            check_id_base=text("leyu_check_id_base"), batch_size=batch_size,
            concurrency=concurrency, batch_interval=interval, **kwargs,
        )

    def headers(self) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/plain, */*", "accept-language": "zh-CN,zh;q=0.9",
            "lang": "zh", "request-code": '{"panda-bss-source":"2"}',
            "requestId": self.request_id,
            "checkId": f"pc-{self.check_id_base}-{self.cuid}-{int(time.time() * 1000)}",
            "origin": self.origin, "referer": self.origin + "/", "user-agent": self.user_agent,
        }
        cookie = self.cookie
        if self.token and "x-api-token=" not in cookie.lower():
            cookie = (cookie + "; " if cookie else "") + "X-API-TOKEN=" + self.token
        if cookie:
            headers["cookie"] = cookie
        return headers

    async def _request(self, client: httpx.AsyncClient, method: str, path: str, **kwargs) -> Any:
        # Never include URL/body/upstream text in raised diagnostics: some include account tokens.
        try:
            async with client.stream(
                method, self.base_url + path, params={"t": int(time.time() * 1000)},
                headers=self.headers(), **kwargs,
            ) as response:
                if response.status_code == 429:
                    raise RateLimited("http_rate_limited", "429")
                if response.status_code in {401, 403}:
                    # HTTP 403 alone does not prove token expiration.
                    raise LeyuError("http_access_denied", str(response.status_code))
                if response.status_code >= 400:
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
                return decode_envelope(payload)
        except httpx.TimeoutException:
            raise LeyuError("upstream_timeout") from None
        except httpx.HTTPError:
            raise LeyuError("upstream_transport_error") from None

    async def fetch(self) -> list[dict[str, Any]]:
        try:
            async with asyncio.timeout(120):
                return await self._fetch()
        except TimeoutError:
            raise LeyuError("collection_deadline_exceeded") from None

    async def _fetch(self) -> list[dict[str, Any]]:
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
            mids = list(by_id)
            failures: list[dict[str, Any]] = []
            semaphore = asyncio.Semaphore(self.concurrency)
            rate_limit = asyncio.Lock()
            next_request = 0.0

            async def fetch_batch(batch: list[str]) -> None:
                nonlocal next_request
                async with semaphore:
                    async with rate_limit:
                        wait = next_request - time.monotonic()
                        if wait > 0:
                            await asyncio.sleep(wait)
                        next_request = time.monotonic() + self.batch_interval
                    try:
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
                                fixture["markets_complete"] = False
                                fixture["collection_error"] = "fixture_detail_missing"
                                continue
                            parsed = parse_fixtures([row], received_at=captured)
                            if parsed:
                                # 详情可补比分/阶段，但不能把详情缺联赛或项目覆盖赛程映射。
                                detail = parsed[0]
                                for key in ("phase", "match_state", "home_score", "away_score", "half_home_score",
                                            "half_away_score", "clock_minute", "clock_raw", "source_updated_at",
                                            "source_time_quality", "received_at"):
                                    fixture[key] = detail[key]
                            fixture["markets"] = parse_quotes(row, fixture["id"], captured)
                            fixture["markets_complete"] = isinstance(row.get("hpsData"), (list, Mapping))
                            if not fixture["markets_complete"]:
                                fixture["collection_error"] = "market_container_missing"
                    except (AuthExpired, RateLimited):
                        raise
                    except LeyuError as exc:
                        failures.append({"size": len(batch), "category": exc.category, "code": exc.code})
                        for mid in batch:
                            by_id[mid]["markets_complete"] = False
                            by_id[mid]["collection_error"] = exc.category

            tasks = [asyncio.create_task(fetch_batch(mids[start:start + self.batch_size]))
                     for start in range(0, len(mids), self.batch_size)]
            try:
                if tasks:
                    await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            self.last_report = {
                "fixtures": len(fixtures),
                "complete_market_fixtures": sum(bool(f.get("markets_complete")) for f in fixtures),
                "failed_batches": failures,
                "received_at": timestamp(datetime.now(timezone.utc)),
            }
            return fixtures
