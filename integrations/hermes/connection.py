"""The administrator-managed OpenAI-compatible connection for this profile."""

import asyncio
import base64
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import hmac
import json
import os
import re
from urllib.parse import urlsplit

from aiohttp import web
from cryptography.fernet import Fernet, InvalidToken
import httpx

STORAGE = "http://api:8080/internal/hermes-connection/storage"
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}")
MANAGED_REQUEST = ContextVar("managed_hermes_connection", default=None)
MANAGED_PROVIDERS = (None, "", "custom", "custom:cline")
INFERENCE_PATHS = {"/v1/chat/completions", "/v1/responses", "/v1/runs", "/api/sessions/{session_id}/chat",
                   "/api/sessions/{session_id}/chat/stream"}


def audit_token(token):
    return hmac.digest(token.encode(), b"hermes-audit-snapshot-v1", "sha256").hex()


def connection_cipher(storage_key):
    key = bytes.fromhex(storage_key)
    if len(key) != 32:
        raise ValueError("Hermes connection storage key must be 32-byte hex")
    return Fernet(base64.urlsafe_b64encode(key))


@dataclass(frozen=True)
class Connection:
    revision: int
    base_url: str
    model: str
    api_key: str | None = field(repr=False)
    updated_at: str | None = None
    verified_at: str | None = None

    def public(self):
        return {
            "revision": self.revision, "base_url": self.base_url, "model": self.model,
            "key_configured": bool(self.api_key), "updated_at": self.updated_at, "verified_at": self.verified_at,
            "application_state": "unconfigured" if not self.api_key else "applied",
        }

    def runtime(self):
        if not self.api_key:
            raise ValueError("Hermes connection API key is not configured")
        return {"provider": "custom", "requested_provider": "custom", "api_mode": "chat_completions",
                "api_key": self.api_key, "base_url": self.base_url, "credential_pool": None,
                "capabilities": {"vision": True}}


def open_connection(cipher, sealed):
    return Connection(**json.loads(cipher.decrypt(sealed.encode())))


class ConnectionStateError(Exception):
    pass


def base_url(value, baseline):
    if not isinstance(value, str) or not value or len(value) > 1024 or not value.isascii() or any(
        ord(c) <= 32 or ord(c) == 127 or c in "\\?#" for c in value
    ):
        raise ValueError("请输入有效的模型服务地址")
    try:
        url = urlsplit(value)
        original = urlsplit(baseline)
        port = url.port
        same_http_gateway = (
            url.scheme == original.scheme == "http"
            and (url.hostname, port or 80) == (original.hostname, original.port or 80)
        )
    except ValueError:
        raise ValueError("请输入有效的模型服务地址") from None
    if (not url.hostname or url.username is not None or url.password is not None or
            port == 0 or (url.scheme != "https" and not same_http_gateway)):
        raise ValueError("模型服务地址须使用 HTTPS，现有 HTTP 网关可保持原主机与端口")
    return value.rstrip("/")


async def probe(config):
    try:
        async with asyncio.timeout(8), httpx.AsyncClient(timeout=5, trust_env=False) as client:
            async with client.stream(
                "POST", config.base_url + "/chat/completions",
                headers={"Authorization": "Bearer " + config.api_key},
                json={"model": config.model, "messages": [{"role": "user", "content": "Reply OK."}], "stream": False},
            ) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=4096):
                    body.extend(chunk)
                    if len(body) > 65536:
                        raise ValueError
                message = json.loads(body)["choices"][0]["message"]
                if (not isinstance(message, dict) or not isinstance(message.get("content"), str)
                        or not message["content"].strip()):
                    raise ValueError
    except httpx.HTTPStatusError as exc:
        raise ValueError(f"模型服务验证失败（HTTP {exc.response.status_code}）") from None
    except (httpx.HTTPError, TimeoutError, ValueError, KeyError, IndexError, TypeError):
        raise ValueError("模型服务超时、无法连接或返回格式异常") from None


class ConnectionSettings:
    def __init__(self, token, storage_key):
        try:
            self.cipher = connection_cipher(storage_key)
        except ValueError:
            raise RuntimeError("Hermes connection storage key must be 32-byte hex") from None
        self.token = token
        self.audit_token = audit_token(token)
        self.audit_cipher = connection_cipher(self.audit_token)
        self.baseline = Connection(0, os.environ["HERMES_PROVIDER_BASE_URL"].rstrip("/"),
                                   os.environ["HERMES_MODEL"], os.environ.get("HERMES_PROVIDER_API_KEY") or None)
        self.current = self.baseline
        self.sealed = None
        self.lock = asyncio.Lock()

    async def storage(self, method, **kwargs):
        async with asyncio.timeout(3), httpx.AsyncClient(timeout=3, trust_env=False) as client:
            response = await client.request(method, STORAGE, headers={"Authorization": "Bearer " + self.token}, **kwargs)
            response.raise_for_status()
            return response.json()

    async def refresh(self):
        data = await self.storage("GET")
        sealed = data["sealed"]
        if sealed != self.sealed:
            stored = open_connection(self.cipher, sealed) if sealed else self.baseline
            if stored.revision <= self.current.revision:
                raise ConnectionStateError("stored Hermes connection did not advance")
            self.current, self.sealed = stored, sealed

    async def online(self):
        async with self.lock:
            await self.refresh()
            return self.current

    async def audit_snapshot(self, request):
        supplied = request.headers.get("X-Hermes-Audit-Token", "")
        if not hmac.compare_digest(supplied.encode(), self.audit_token.encode()):
            return web.json_response({"detail": "invalid audit credential"}, status=401)
        try:
            async with self.lock:
                await self.refresh()
                if not self.sealed or not self.current.api_key:
                    return web.json_response({"detail": "managed connection is unavailable"}, status=503)
                snapshot = self.audit_cipher.encrypt(json.dumps(asdict(self.current)).encode()).decode()
                return web.json_response({"sealed": snapshot}, headers={"Cache-Control": "no-store"})
        except (httpx.HTTPError, TimeoutError, InvalidToken, ValueError, ConnectionStateError):
            return web.json_response({"detail": "managed connection is unavailable"}, status=503)

    def candidate(self, data):
        if not isinstance(data, dict) or set(data) - {"revision", "base_url", "model", "api_key", "clear_key"}:
            raise ValueError("连接配置字段无效")
        current = self.current
        url = base_url(data.get("base_url"), self.baseline.base_url)
        model = data.get("model")
        if not isinstance(model, str) or not MODEL_ID.fullmatch(model) or "://" in model or model == "hermes-agent":
            raise ValueError("请输入服务商提供的模型标识")
        key = data.get("api_key", "")
        clear = data.get("clear_key", False)
        if (type(clear) is not bool or not isinstance(key, str) or len(key) > 4096
                or any(not 33 <= ord(c) <= 126 for c in key) or (clear and key)):
            raise ValueError("密钥格式无效")
        if url != current.base_url and not key:
            raise ValueError("更换模型服务地址时，请输入该地址对应的 API 密钥")
        return Connection(current.revision + 1, url, model, None if clear else key or current.api_key)

    async def update(self, request):
        data = await request.json()
        if not isinstance(data, dict) or type(data.get("revision")) is not int:
            raise ValueError("请刷新后提交当前配置版本")
        if data["revision"] != self.current.revision:
            return web.json_response({"detail": "连接配置已更新，请刷新后重试"}, status=409)
        candidate = self.candidate(data)
        if not candidate.api_key and not data.get("clear_key"):
            raise ValueError("请填写 API 密钥")
        if candidate.api_key:
            await probe(candidate)
        now = datetime.now(timezone.utc).isoformat()
        candidate = replace(candidate, updated_at=now, verified_at=now if candidate.api_key else None)
        sealed = self.cipher.encrypt(json.dumps(asdict(candidate)).encode()).decode()
        try:
            await self.storage("PUT", json={
                "sealed": sealed, "previous": self.sealed, "revision": candidate.revision,
                "actor": request.headers.get("X-Hermes-Connection-Actor", ""),
            })
        except (httpx.HTTPError, TimeoutError, ValueError):
            await self.refresh()
            if self.sealed != sealed:
                return web.json_response({"detail": "保存结果待核对，请刷新连接状态"}, status=503)
        self.current, self.sealed = candidate, sealed
        return web.json_response(candidate.public())

    async def handle(self, request):
        token = request.headers.get("X-Hermes-Connection-Token", "")
        if not hmac.compare_digest(token.encode(), self.token.encode()):
            return web.json_response({"detail": "服务认证失败"}, status=401)
        try:
            async with self.lock:
                await self.refresh()
                if request.method == "GET":
                    return web.json_response(self.current.public())
                if request.content_length is None or request.content_length > 8192:
                    raise ValueError("连接配置请求超出长度限制")
                return await self.update(request)
        except ValueError as exc:
            detail = "连接配置格式无效" if isinstance(exc, json.JSONDecodeError) else str(exc)
            return web.json_response({"detail": detail}, status=422)
        except (httpx.HTTPError, TimeoutError, InvalidToken, ConnectionStateError):
            return web.json_response({"detail": "暂时无法读取或保存连接配置"}, status=503)


def install():
    token = os.environ.get("HERMES_CONNECTION_TOKEN", "")
    if not token:
        return
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise RuntimeError("Hermes connection token must be 32-byte hex")
    from gateway.platforms.api_server import APIServerAdapter, _ProviderAuthResolutionError
    from gateway import run as gateway_run

    storage_key = os.environ.get("HERMES_CONNECTION_STORAGE_KEY", "")
    if token == storage_key or token == os.environ.get("API_SERVER_KEY"):
        raise RuntimeError("Hermes connection token must be distinct from other credentials")
    settings = ConnectionSettings(token, storage_key)
    routes = APIServerAdapter._http_route_table
    connect = APIServerAdapter.connect
    select = APIServerAdapter._select_agent_runtime
    create_agent = APIServerAdapter._create_agent
    resolve = gateway_run._resolve_runtime_agent_kwargs

    def managed_runtime():
        active = MANAGED_REQUEST.get()
        return active.runtime() | {"model": active.model} if active else resolve()

    def create_with_settings(adapter, *args, **kwargs):
        if MANAGED_REQUEST.get() is not None:
            kwargs["confirmed_runtime_lock"] = True
        return create_agent(adapter, *args, **kwargs)

    def route_table(adapter):
        def managed_inference(handler):
            async def inference(request):
                denied = adapter._check_auth(request)
                if denied is not None:
                    return denied
                try:
                    active = await settings.online()
                    if not active.api_key:
                        raise ConnectionStateError("Hermes connection key is unavailable")
                except (httpx.HTTPError, TimeoutError, InvalidToken, ValueError, ConnectionStateError):
                    return web.json_response({"error": {"message": "Hermes connection is unavailable",
                                                         "type": "configuration_error"}}, status=503)
                if active.revision and request.can_read_body:
                    try:
                        body = await request.json()
                    except (ValueError, UnicodeError):
                        body = None
                    if isinstance(body, dict) and body.get("provider") not in MANAGED_PROVIDERS:
                        return web.json_response({"error": {"message": "Hermes provider is managed",
                                                             "type": "invalid_request_error"}}, status=400)
                context = MANAGED_REQUEST.set(active if active.revision else None)
                try:
                    response = await handler(request)
                    if not response.prepared:
                        response.headers["X-Hermes-Connection-Revision"] = str(active.revision)
                    return response
                finally:
                    MANAGED_REQUEST.reset(context)

            return inference

        return [
            (method, path, managed_inference(handler)
             if method == "POST" and path in INFERENCE_PATHS else handler)
            for method, path, handler in routes(adapter)
        ] + [(method, "/project/connection", settings.handle) for method in ("GET", "PUT")] + [
            ("GET", "/project/connection/audit-snapshot", settings.audit_snapshot),
        ]

    async def connect_with_settings(adapter, **kwargs):
        await settings.refresh()
        return await connect(adapter, **kwargs)

    def select_with_settings(adapter, runtime_kwargs, model, **kwargs):
        active = MANAGED_REQUEST.get()
        if active is None:
            return select(adapter, runtime_kwargs, model, **kwargs)
        requested_provider = kwargs.get("requested_provider")
        if requested_provider not in MANAGED_PROVIDERS:
            raise _ProviderAuthResolutionError("Hermes provider is controlled by the administrator")
        requested_model = kwargs.get("requested_model")
        model = requested_model if requested_model and requested_model != "hermes-agent" else active.model
        return model, None, requested_model, requested_provider

    APIServerAdapter._http_route_table = route_table
    APIServerAdapter.connect = connect_with_settings
    APIServerAdapter._create_agent = create_with_settings
    APIServerAdapter._select_agent_runtime = select_with_settings
    gateway_run._resolve_runtime_agent_kwargs = managed_runtime
