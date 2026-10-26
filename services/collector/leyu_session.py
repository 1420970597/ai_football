"""App 续期、动态网关与短期登录代理。持久互斥和秘密写入由远端调用方提供。"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import secrets
import socket
import time
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import httpx

from .leyu import safe_base_url
from .leyu_protocol import LeyuError

MAX_APP_BYTES = 2 * 1024 * 1024
MAX_DISCOVERY_BYTES = 256 * 1024
MAX_PROXY_BYTES = 64 * 1024
MAX_PROXY_LIFETIME = 60.0
PROXY_MARGIN = 5.0
LOGIN_PATH = "/site/api/v1/member/login"
PRE_INFO_PATH = "/site/api/v1/configuration/preInfo"
LAUNCH_PATH = "/game/api/v1/venue/launch"
APP_VERSION = "2.0.1"
SITE_ID = "2001"


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


async def bounded_request(client: httpx.AsyncClient, method: str, url: str, *, limit: int = MAX_APP_BYTES, **kwargs) -> bytes:
    try:
        async with client.stream(method, url, **kwargs) as response:
            if response.status_code >= 300:
                raise LeyuError("app_http_rejected", str(response.status_code))
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > limit:
                    raise LeyuError("app_response_too_large")
            return bytes(body)
    except httpx.TimeoutException:
        raise LeyuError("app_timeout") from None
    except httpx.HTTPError:
        raise LeyuError("app_transport_failure") from None


async def app_post(client: httpx.AsyncClient, host: str, path: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    raw = await bounded_request(client, "POST", host + path, headers=headers, json=body)
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
        self._candidates: list[str] = []
        self._next_refresh = 0.0
        self._lock = asyncio.Lock()

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
                    if self.verify_public:
                        await public_dns(str(feed))
                    raw = await bounded_request(client, "GET", str(feed), limit=MAX_DISCOVERY_BYTES)
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
    """每次续期提取一个短期租约；不修改全局代理或普通采集客户端。"""

    def __init__(self, config: Mapping[str, Any]):
        self.config = config

    async def acquire(self, direct: httpx.AsyncClient) -> ProxyLease | None:
        url = str(self.config.get("leyu_login_proxy_url") or "").strip()
        extract = str(self.config.get("leyu_login_proxy_extract_url") or "").strip()
        if not url and not extract:
            return None
        started = time.monotonic()
        deadline = started + MAX_PROXY_LIFETIME - PROXY_MARGIN
        egress = ""
        if extract:
            parts = urlsplit(extract)
            if parts.scheme != "https" or not parts.netloc or parts.username or parts.password:
                raise LeyuError("invalid_proxy_extraction_url")
            raw = await bounded_request(direct, "GET", extract, limit=MAX_PROXY_BYTES)
            text = raw.decode("utf-8", "strict").strip()
            if text.startswith("{"):
                try:
                    payload = json.loads(text)
                    if payload.get("code") != "SUCCESS" or len(payload.get("data", [])) != 1:
                        raise ValueError("not one lease")
                    lease = payload["data"][0]
                    url = str(lease["server"])
                    egress = str(lease.get("proxy_ip") or "")
                    if lease.get("deadline"):
                        expires = datetime.strptime(lease["deadline"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=8)))
                        deadline = min(deadline, time.monotonic() + expires.timestamp() - time.time() - PROXY_MARGIN)
                except (KeyError, TypeError, ValueError):
                    raise LeyuError("invalid_proxy_lease") from None
            else:
                url = text
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
        # 对当前配置提供方采用其短期会话口令，保证出口探测和登录共享一次动态出口。
        # session 是随机租约标识，并非固定出口 IP；旧配置可显式关闭/覆盖模板。
        template = str(self.config.get("leyu_login_proxy_password_template") or "")
        if not template and (parts.hostname == "iproyal.com" or parts.hostname.endswith(".iproyal.com")) and "_session-" not in unquote(parts.password or ""):
            # 提供方FAQ秒单位上限59；本地租约再预留5秒，不延长为长期出口。
            template = "{password}_session-{session}_lifetime-59s_killswitch-1"
        if template:
            if any(field not in {"password", "session"} for field in re.findall(r"\{([^{}]+)\}", template)):
                raise LeyuError("invalid_proxy_password_template")
            password = template.format(password=unquote(parts.password or ""), session=secrets.token_hex(4))
            user = quote(unquote(parts.username or ""), safe="")
            hostname = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
            url = urlunsplit((parts.scheme, f"{user}:{quote(password, safe='')}@{hostname}:{parts.port}", "", "", ""))
        lease = ProxyLease(url, deadline, egress)
        if egress:
            try:
                if not ipaddress.ip_address(egress).is_global:
                    raise ValueError("nonpublic")
            except ValueError:
                raise LeyuError("invalid_proxy_egress_ip") from None
        lease.remaining()
        return lease


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
                   "x-api-token": token, "x-api-currency": "CNY", "content-type": "application/json;charset=utf-8",
                   "user-agent": "okhttp/4.12.0"}
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
            if exc.category not in {"app_http_rejected", "app_transport_failure", "app_timeout"}:
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
        lease = await LoginProxy(self.config).acquire(direct)
        if lease:
            transport = self.login_transport_factory(lease.url) if self.login_transport_factory else None
            proxy = None if transport else lease.url
            timeout = httpx.Timeout(min(20, lease.remaining()), connect=min(5, lease.remaining()))
            async with asyncio.timeout(lease.remaining()), httpx.AsyncClient(timeout=timeout, proxy=proxy, transport=transport, trust_env=False, follow_redirects=False) as login:
                if not lease.egress_ip:
                    # 只在登录事务中为同一短期出口验证一次 IP，不代理 preInfo/场馆/采集。
                    check_url = str(self.config.get("leyu_login_proxy_egress_url") or "https://ipv4.icanhazip.com/")
                    if urlsplit(check_url).scheme != "https":
                        raise LeyuError("invalid_proxy_egress_check_url")
                    raw = await bounded_request(login, "GET", check_url, limit=128)
                    try:
                        ip = ipaddress.ip_address(raw.decode("ascii").strip())
                        if not ip.is_global:
                            raise ValueError("nonpublic")
                        lease.egress_ip = str(ip)
                    except (ValueError, UnicodeError):
                        raise LeyuError("invalid_proxy_egress_ip") from None
                remaining = lease.remaining()
                if remaining <= 1:
                    raise LeyuError("login_proxy_expired")
                login.timeout = httpx.Timeout(min(20, remaining), connect=min(5, remaining))
                return await self._submit_login(login, host, lease.egress_ip, name, password)
        return await self._submit_login(direct, host, direct_ip, name, password)

    async def _submit_login(self, client: httpx.AsyncClient, host: str, ip: str, name: str, password: str) -> str:
        body = {"uuid": self.config["leyu_app_uuid"], "name": name,
                "password": hashlib.new("md5", password.encode()).hexdigest(),
                "Flag": 1, "Version": APP_VERSION, "Kaptchcate": 99}
        headers = self._headers("/site/api", ip)
        self._login_submitted = True
        payload = await app_post(client, host, LOGIN_PATH, headers, body)
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
