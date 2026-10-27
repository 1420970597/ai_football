"""App 续期、动态网关与短期登录代理。持久互斥和秘密写入由远端调用方提供。"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import errno
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import secrets
import socket
import string
import time
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import httpx

from .leyu import safe_base_url
from .leyu_protocol import LeyuError

MAX_APP_BYTES = 2 * 1024 * 1024
MAX_DISCOVERY_BYTES = 256 * 1024
MAX_PROXY_LIFETIME = 60.0
PROXY_MARGIN = 5.0
PROXY_EGRESS_URL = "https://ipv4.icanhazip.com/"
LOGIN_PATH = "/site/api/v1/user/login"
PRE_INFO_PATH = "/site/api/v1/configuration/preInfo"
LAUNCH_PATH = "/game/api/v1/venue/launch"
APP_VERSION = "2.0.1"
SITE_ID = "2001"


def _connect_failure_category(scope: str, exc: BaseException) -> str:
    cause = exc.__cause__
    for _ in range(4):
        if cause is None:
            break
        if isinstance(cause, socket.gaierror):
            return f"{scope}_dns_failure"
        code = getattr(cause, "errno", None)
        kind = {
            errno.ECONNREFUSED: "connection_refused",
            errno.ENETUNREACH: "network_unreachable",
            errno.EHOSTUNREACH: "host_unreachable",
            errno.ETIMEDOUT: "connect_timeout",
            errno.ECONNRESET: "connection_reset",
        }.get(code)
        if kind:
            return f"{scope}_{kind}"
        cause = cause.__cause__
    return f"{scope}_connect_failure"


def private_document(path: str) -> dict[str, Any]:
    if not path:
        return {}
    try:
        file = Path(path)
        if file.stat().st_size > MAX_DISCOVERY_BYTES:
            raise ValueError("oversize")
        document = json.loads(file.read_text())
        if not isinstance(document, dict):
            raise ValueError("not object")
        return document
    except (OSError, ValueError, UnicodeError):
        raise LeyuError("private_config_unreadable") from None


def aes_cbc(data: bytes, key: bytes, iv: bytes, *, decrypt: bool = False) -> bytes:
    if len(key) not in {16, 24, 32} or len(iv) != 16:
        raise LeyuError("invalid_signing_key")
    if decrypt:
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        unpadder = padding.PKCS7(128).unpadder()
        try:
            return unpadder.update(cipher.update(data) + cipher.finalize()) + unpadder.finalize()
        except ValueError:
            raise LeyuError("invalid_discovery_ciphertext") from None
    padder = padding.PKCS7(128).padder()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return cipher.update(padder.update(data) + padder.finalize()) + cipher.finalize()


class AppSigner:
    def __init__(self, routes: Mapping[str, Any]):
        self._routes = dict(routes)
        for route in ("", "/site/api", "/game/api"):
            try:
                entry = self._routes[route]
                key, iv = str(entry["key"]).encode(), str(entry["iv"]).encode()
                if len(key) not in {16, 24, 32} or len(iv) != 16:
                    raise ValueError("invalid length")
            except (KeyError, TypeError, ValueError):
                raise LeyuError("invalid_signing_config") from None

    def headers(self, prefix: str, client_ip: str) -> dict[str, str]:
        try:
            ipaddress.ip_address(client_ip)
        except ValueError:
            raise LeyuError("invalid_signing_ip") from None
        stamp = str(int(time.time() * 1000)) + str(secrets.randbelow(900) + 100)
        bound = json.dumps({"tt": str(int(time.time())), "xx": client_ip}, separators=(",", ":"))
        def encrypted(route: str, value: str) -> bytes:
            spec = self._routes[route]
            return aes_cbc(value.encode(), spec["key"].encode(), spec["iv"].encode())
        return {"x-api-xxx": encrypted(prefix, stamp).hex(),
                "x-api-hack-xxxxx": base64.b64encode(encrypted("", bound)).decode()}


async def public_dns(url: str) -> None:
    parts = urlsplit(url)
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ValueError("nonpublic")
    except (OSError, ValueError):
        raise LeyuError("untrusted_discovery_host") from None


async def bounded_request(client: httpx.AsyncClient, method: str, url: str, *, limit: int = MAX_APP_BYTES,
                          failure_scope: str = "app", **kwargs) -> bytes:
    try:
        async with client.stream(method, url, **kwargs) as response:
            if response.status_code >= 300:
                raise LeyuError(f"{failure_scope}_http_rejected", str(response.status_code))
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > limit:
                    raise LeyuError("app_response_too_large")
            return bytes(body)
    except httpx.ProxyError as exc:
        # httpcore exposes a CONNECT response as e.g. "407 Proxy ...". Keep
        # only the numeric status; never persist the provider's reason text.
        match = re.search(r"\b([1-5][0-9]{2})\b", str(exc))
        if match:
            raise LeyuError(f"{failure_scope}_proxy_http_rejected", match.group(1)) from None
        raise LeyuError(f"{failure_scope}_proxy_failure") from None
    except httpx.ConnectTimeout:
        raise LeyuError(f"{failure_scope}_connect_timeout") from None
    except httpx.ReadTimeout:
        raise LeyuError(f"{failure_scope}_read_timeout") from None
    except httpx.TimeoutException:
        raise LeyuError(f"{failure_scope}_timeout") from None
    except httpx.ConnectError as exc:
        raise LeyuError(_connect_failure_category(failure_scope, exc)) from None
    except httpx.ReadError:
        raise LeyuError(f"{failure_scope}_read_failure") from None
    except httpx.HTTPError:
        raise LeyuError(f"{failure_scope}_transport_failure") from None


async def app_post(client: httpx.AsyncClient, host: str, path: str, headers: dict[str, str],
                   body: dict[str, Any], *, failure_scope: str = "app") -> dict[str, Any]:
    encoded = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    raw = await bounded_request(client, "POST", host + path, headers=headers, content=encoded,
                                failure_scope=failure_scope)
    try:
        data = json.loads(raw)
        status = data.get("status_code")
        if isinstance(status, bool) or not re.fullmatch(r"\d{1,8}", str(status)):
            raise ValueError("invalid status")
        return data
    except (AttributeError, ValueError, UnicodeError):
        raise LeyuError("invalid_app_response") from None


class DomainResolver:
    def __init__(self, document: Mapping[str, Any], *, verify_public: bool = True):
        self.document = dict(document)
        self.verify_public = verify_public
        redirect_hosts = self.document.get("redirect_hosts", [])
        if not isinstance(redirect_hosts, list) or len(redirect_hosts) > 16:
            raise LeyuError("invalid_domain_redirects")
        self.redirect_hosts = {str(host).strip().lower() for host in redirect_hosts if str(host).strip()}
        self._candidates: list[str] = []
        self._next_refresh = 0.0
        self._lock = asyncio.Lock()

    async def _fetch_feed(self, client: httpx.AsyncClient, feed: str) -> bytes:
        parts = urlsplit(feed)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise LeyuError("invalid_discovery_feed")
        allowed_hosts = self.redirect_hosts | {parts.hostname.lower()}
        current = feed
        for redirect_count in range(5):
            current_parts = urlsplit(current)
            if (current_parts.scheme != "https" or not current_parts.hostname
                    or current_parts.username or current_parts.password
                    or current_parts.hostname.lower() not in allowed_hosts):
                raise LeyuError("untrusted_discovery_redirect")
            if self.verify_public:
                await public_dns(current)
            try:
                async with client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location", "")
                        if not location or redirect_count == 4:
                            raise LeyuError("invalid_discovery_redirect")
                        current = urljoin(current, location)
                        continue
                    if response.status_code >= 300:
                        raise LeyuError("app_http_rejected", str(response.status_code))
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_DISCOVERY_BYTES:
                            raise LeyuError("app_response_too_large")
                    return bytes(body)
            except httpx.TimeoutException:
                raise LeyuError("app_timeout") from None
            except httpx.HTTPError:
                raise LeyuError("app_transport_failure") from None
        raise LeyuError("invalid_discovery_redirect")

    async def candidates(self, client: httpx.AsyncClient) -> list[str]:
        async with self._lock:
            if time.monotonic() < self._next_refresh:
                return self._candidates[:]
            feeds = self.document.get("feeds", [])
            if not isinstance(feeds, list) or len(feeds) > 4:
                raise LeyuError("invalid_domain_feeds")
            errors = 0
            for feed in feeds:
                try:
                    parts = urlsplit(str(feed))
                    if parts.scheme != "https" or not parts.netloc or parts.username or parts.password:
                        raise LeyuError("invalid_discovery_feed")
                    raw = await self._fetch_feed(client, str(feed))
                    key, iv = self.document.get("key"), self.document.get("iv")
                    if key and iv:
                        raw = aes_cbc(base64.b64decode(b"".join(raw.split()), validate=True), str(key).encode(), str(iv).encode(), decrypt=True)
                    document = json.loads(raw)
                    group = document.get("domain_group")
                    if not isinstance(group, list):
                        raise LeyuError("invalid_domain_document")
                    candidates = [safe_base_url(str(row.get("domain", ""))) for row in group[:16] if isinstance(row, Mapping)]
                    if candidates:
                        self._candidates = list(dict.fromkeys(candidates))
                        self._next_refresh = time.monotonic() + max(30, min(3600, float(self.document.get("refresh_s", 300))))
                        return self._candidates[:]
                except (LeyuError, ValueError, UnicodeError, TypeError):
                    errors += 1
            self._next_refresh = time.monotonic() + 30
            seeds = self.document.get("seed_hosts", [])
            if not isinstance(seeds, list):
                raise LeyuError("invalid_domain_seeds")
            self._candidates = list(dict.fromkeys(safe_base_url(str(value)) for value in seeds[:16]))
            if errors and not self._candidates:
                raise LeyuError("domain_discovery_unavailable")
            return self._candidates[:]


@dataclass(repr=False)
class ProxyLease:
    url: str
    deadline: float
    egress_ip: str = ""

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise LeyuError("login_proxy_expired")
        return remaining


class LoginProxy:
    """建立仅用于一次 App 登录的代理租约；普通采集始终使用直连客户端。"""

    def __init__(self, config: Mapping[str, Any]):
        self.config = config

    @staticmethod
    def _validate_password_template(template: str) -> None:
        try:
            fields = [field for _, field, spec, conversion in string.Formatter().parse(template)
                      if field is not None and (field not in {"password", "session"} or spec or conversion)]
        except ValueError:
            raise LeyuError("invalid_proxy_password_template") from None
        if fields:
            raise LeyuError("invalid_proxy_password_template")

    def validate_configuration(self) -> None:
        url = str(self.config.get("leyu_login_proxy_url") or "").strip()
        if url:
            candidate = url if "://" in url else "http://" + url
            try:
                parts = urlsplit(candidate)
                valid = (parts.scheme in {"http", "https"} and parts.hostname and parts.port
                         and parts.path in {"", "/"} and not parts.query and not parts.fragment)
            except ValueError:
                valid = False
            if not valid:
                raise LeyuError("invalid_login_proxy")
        template = str(self.config.get("leyu_login_proxy_password_template") or "")
        self._validate_password_template(template)

    async def acquire(self) -> ProxyLease | None:
        self.validate_configuration()
        url = str(self.config.get("leyu_login_proxy_url") or "").strip()
        if not url:
            return None
        started = time.monotonic()
        deadline = started + MAX_PROXY_LIFETIME - PROXY_MARGIN
        if "://" not in url:
            url = "http://" + url
        try:
            parts = urlsplit(url)
            valid = (parts.scheme in {"http", "https"} and parts.hostname and parts.port
                     and parts.path in {"", "/"} and not parts.query and not parts.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise LeyuError("invalid_login_proxy")
        # Session-affinity providers require one short-lived identity for the login request.
        # The lease is discarded after this request; ordinary traffic stays direct.
        template = str(self.config.get("leyu_login_proxy_password_template") or "")
        if not template and (parts.hostname == "iproyal.com" or parts.hostname.endswith(".iproyal.com")) and "_session-" not in unquote(parts.password or ""):
            # 提供方FAQ秒单位上限59；本地租约再预留5秒，不延长为长期出口。
            template = "{password}_session-{session}_lifetime-59s_killswitch-1"
        if template:
            self._validate_password_template(template)
            password = template.format(password=unquote(parts.password or ""), session=secrets.token_hex(4))
            user = quote(unquote(parts.username or ""), safe="")
            hostname = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
            url = urlunsplit((parts.scheme, f"{user}:{quote(password, safe='')}@{hostname}:{parts.port}", "", "", ""))
        lease = ProxyLease(url, deadline)
        lease.remaining()
        return lease

    @staticmethod
    async def resolve_egress_ip(client: httpx.AsyncClient) -> str:
        raw = await bounded_request(client, "GET", PROXY_EGRESS_URL, limit=128,
                                    failure_scope="proxy_egress")
        try:
            value = raw.decode("ascii").strip()
            address = ipaddress.ip_address(value)
            if not address.is_global:
                raise ValueError("nonpublic")
            return str(address)
        except (UnicodeError, ValueError):
            raise LeyuError("invalid_proxy_egress_ip") from None


Claim = Callable[[str], Awaitable[str]]
Finish = Callable[[str, str, dict[str, Any]], Awaitable[None]]
Persist = Callable[[dict[str, str]], Awaitable[None]]


class AppSessionRenewer:
    def __init__(self, config: Mapping[str, Any], *, claim: Claim, finish: Finish, persist: Persist,
                 direct_transport: httpx.AsyncBaseTransport | None = None,
                 login_transport_factory: Callable[[str], httpx.AsyncBaseTransport] | None = None,
                 verify_public_hosts: bool = True):
        self.config = dict(config)
        self.claim = claim
        self.finish = finish
        self.persist = persist
        self.direct_transport = direct_transport
        self.login_transport_factory = login_transport_factory
        self.verify_public_hosts = verify_public_hosts
        self._lock = asyncio.Lock()
        self._login_submitted = False
        signing = private_document(str(config.get("leyu_app_signing_config") or ""))
        self.signer = AppSigner(signing["routes"]) if signing.get("routes") else None
        discovery = private_document(str(config.get("leyu_app_discovery_config") or ""))
        self.resolver = DomainResolver(discovery, verify_public=verify_public_hosts)

    def _headers(self, prefix: str, ip: str, token: str = "") -> dict[str, str]:
        headers = {"x-api-client": "android" if prefix == "/site/api" else "sport_android",
                   "x-api-version": APP_VERSION, "x-api-site": SITE_ID, "x-api-language": "CHS",
                   "x-api-uuid": str(self.config.get("leyu_app_uuid") or ""),
                   "x-api-token": token, "x-api-currency": "CNY", "content-type": "application/json; charset=utf-8",
                   "user-agent": "okhttp/4.12.0", "accept-encoding": "gzip"}
        if self.signer:
            headers.update(self.signer.headers(prefix, ip))
        else:
            key = "leyu_app_login_signature" if prefix == "/site/api" else "leyu_app_signature"
            signature = str(self.config.get(key) or "")
            if not signature:
                raise LeyuError("app_signature_missing")
            headers["x-api-xxx"] = signature
        return headers

    async def _prepare(self, client: httpx.AsyncClient, host: str) -> str:
        # preInfo 无口令无 App token，经真实出口执行。初始绑定来自官方 App 固定私网默认值。
        headers = self._headers("/site/api", "192.168.28.123")
        payload = await app_post(client, host, PRE_INFO_PATH, headers, {})
        data = payload.get("data")
        if str(payload.get("status_code")) not in {"6000", "6031"} or not isinstance(data, Mapping):
            raise LeyuError("app_initialization_rejected", str(payload.get("status_code")))
        try:
            return str(ipaddress.ip_address(str(data.get("ip") or "")))
        except ValueError:
            raise LeyuError("app_initialization_ip_missing") from None

    async def _choose_host(self, client: httpx.AsyncClient) -> tuple[str, str]:
        host = safe_base_url(str(self.config.get("leyu_app_host") or ""))
        try:
            return host, await self._prepare(client, host)
        except LeyuError as exc:
            if exc.category not in {
                "app_http_rejected", "app_transport_failure", "app_timeout",
                "app_connect_timeout", "app_read_timeout", "app_connect_failure",
                "app_read_failure", "app_proxy_failure",
            }:
                raise
            candidates = await self.resolver.candidates(client)
            for candidate in candidates[:4]:
                if candidate == host:
                    continue
                try:
                    if self.verify_public_hosts:
                        await public_dns(candidate)
                    return candidate, await self._prepare(client, candidate)
                except LeyuError:
                    continue
            raise LeyuError("no_verified_app_gateway") from None

    async def _login(self, direct: httpx.AsyncClient, host: str, direct_ip: str) -> str:
        name, password = str(self.config.get("leyu_app_login_name") or ""), str(self.config.get("leyu_app_login_password") or "")
        if not name or not password or not self.config.get("leyu_app_uuid"):
            raise LeyuError("app_login_credentials_missing")
        lease = await LoginProxy(self.config).acquire()
        if lease:
            transport = self.login_transport_factory(lease.url) if self.login_transport_factory else None
            proxy = None if transport else lease.url
            timeout = httpx.Timeout(min(20, lease.remaining()), connect=min(5, lease.remaining()))
            async with asyncio.timeout(lease.remaining()), httpx.AsyncClient(timeout=timeout, proxy=proxy, transport=transport, trust_env=False, follow_redirects=False) as login:
                egress_ip = direct_ip
                if self.signer:
                    egress_ip = await LoginProxy.resolve_egress_ip(login)
                remaining = lease.remaining()
                if remaining <= 1:
                    raise LeyuError("login_proxy_expired")
                login.timeout = httpx.Timeout(min(20, remaining), connect=min(5, remaining))
                return await self._submit_login(login, host, egress_ip, name, password,
                                                failure_scope="proxy_login")
        return await self._submit_login(direct, host, direct_ip, name, password, failure_scope="app_login")

    async def _submit_login(self, client: httpx.AsyncClient, host: str, ip: str, name: str, password: str,
                            *, failure_scope: str) -> str:
        body = {"uuid": self.config["leyu_app_uuid"], "name": name,
                "password": hashlib.new("md5", password.encode()).hexdigest(),
                "Flag": 1, "Version": APP_VERSION, "Kaptchcate": 99}
        headers = self._headers("/site/api", ip)
        self._login_submitted = True
        payload = await app_post(client, host, LOGIN_PATH, headers, body, failure_scope=failure_scope)
        if str(payload.get("status_code")) != "6000":
            raise LeyuError("app_login_rejected", str(payload.get("status_code")))
        data = payload.get("data")
        if not isinstance(data, Mapping) or not data.get("token"):
            raise LeyuError("app_login_token_missing")
        return str(data["token"])

    async def _launch(self, client: httpx.AsyncClient, host: str, ip: str, token: str) -> dict[str, Any]:
        body = {"enName": str(self.config.get("leyu_venue") or "YBTY"), "gameCode": "", "isApp": "true",
                "clientType": "android", "https": "true", "isPreload": True, "rollbackAddress": "",
                "sendMoney": "false", "siteId": SITE_ID, "nativeApp": "1", "isPandaAct": "0", "isUseCache": False}
        payload = await app_post(client, host, LAUNCH_PATH, self._headers("/game/api", ip, token), body)
        if str(payload.get("status_code")) == "6001":
            raise LeyuError("app_token_expired", "6001")
        if str(payload.get("status_code")) != "6000":
            raise LeyuError("venue_launch_rejected", str(payload.get("status_code")))
        data = payload.get("data")
        if not isinstance(data, Mapping) or not data.get("url"):
            raise LeyuError("venue_url_missing")
        parts = urlsplit(str(data["url"]))
        if parts.scheme != "https" or not parts.netloc or parts.username or parts.password:
            raise LeyuError("invalid_venue_url")
        request_id = parse_qs(parts.query).get("token", [""])[0]
        if not request_id:
            raise LeyuError("venue_session_missing")
        base = f"https://{parts.netloc}"
        raw_origin = urlsplit(str(data.get("h5Url") or base))
        origin = safe_base_url(f"{raw_origin.scheme}://{raw_origin.netloc}")
        return {"leyu_request_id": request_id, "leyu_api_base": base,
                "leyu_origin": origin}

    async def renew(self, reason: str = "session_expired") -> dict[str, str]:
        try:
            async with asyncio.timeout(150):
                return await self._renew(reason)
        except TimeoutError:
            raise LeyuError("renewal_deadline_exceeded") from None

    async def _renew(self, reason: str) -> dict[str, str]:
        if reason not in {"session_expired", "authorized_bootstrap"}:
            raise LeyuError("renewal_reason_not_authorized")
        async with self._lock:
            # 调用方必须在远端持久化互斥、冷却和未知/拒绝状态；无默认内存回退。
            account = str(self.config.get("leyu_app_login_name") or self.config.get("leyu_app_uuid") or "")
            if not account:
                raise LeyuError("app_identity_missing")
            attempt = await self.claim(hashlib.sha256((SITE_ID + ":" + account.casefold()).encode()).hexdigest())
            if not attempt:
                raise LeyuError("renewal_claim_rejected")
            login_sent = False
            self._login_submitted = False
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5), transport=self.direct_transport,
                                             trust_env=False, follow_redirects=False) as direct:
                    host, ip = await self._choose_host(direct)
                    token = str(self.config.get("leyu_app_token") or self.config.get("leyu_api_token") or "")
                    if token:
                        try:
                            updates = await self._launch(direct, host, ip, token)
                        except LeyuError as exc:
                            if exc.category != "app_token_expired":
                                raise
                            updates = {}
                    else:
                        updates = {}
                    if not updates:
                        token = await self._login(direct, host, ip)
                        login_sent = self._login_submitted
                        await self.persist({"leyu_app_token": token, "leyu_app_host": host})
                        self.config["leyu_app_token"] = token
                        updates = await self._launch(direct, host, ip, token)
                    updates["leyu_app_host"] = host
                    await self.persist(updates)
                    self.config.update(updates)
                    await self.finish(attempt, "success", {"login_submitted": login_sent})
                    return updates
            except BaseException as exc:
                category = exc.category if isinstance(exc, LeyuError) else type(exc).__name__
                code = exc.code if isinstance(exc, LeyuError) else ""
                login_sent = self._login_submitted
                status = "rejected" if category == "app_login_rejected" else "unknown" if login_sent else "failed"
                await asyncio.shield(self.finish(attempt, status, {"category": category, "code": code, "login_submitted": login_sent}))
                raise
