"""The administrator-managed OpenAI-compatible connection for this profile."""

import asyncio
import base64
from dataclasses import asdict, dataclass, replace
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


@dataclass(frozen=True)
class Connection:
    revision: int
    base_url: str
    model: str
    api_key: str | None
    updated_at: str | None = None
    verified_at: str | None = None

    def public(self):
        return {
            "revision": self.revision, "base_url": self.base_url, "model": self.model,
            "key_configured": bool(self.api_key), "updated_at": self.updated_at, "verified_at": self.verified_at,
            "application_state": "unconfigured" if not self.api_key else "applied",
        }


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
            key = bytes.fromhex(storage_key)
            if len(key) != 32:
                raise ValueError
        except ValueError:
            raise RuntimeError("Hermes connection storage key must be 32-byte hex") from None
        self.cipher = Fernet(base64.urlsafe_b64encode(key))
        self.token = token
        self.baseline = Connection(0, os.environ["HERMES_PROVIDER_BASE_URL"].rstrip("/"),
                                   os.environ["HERMES_MODEL"], os.environ.get("HERMES_PROVIDER_API_KEY"))
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
            self.current = Connection(**json.loads(self.cipher.decrypt(sealed.encode()))) if sealed else self.baseline
            self.sealed = sealed

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
        except (httpx.HTTPError, TimeoutError, InvalidToken):
            return web.json_response({"detail": "暂时无法读取或保存连接配置"}, status=503)


def install():
    token = os.environ.get("HERMES_CONNECTION_TOKEN", "")
    if not token:
        return
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise RuntimeError("Hermes connection token must be 32-byte hex")
    from gateway.platforms.api_server import APIServerAdapter, _ProviderAuthResolutionError

    storage_key = os.environ.get("HERMES_CONNECTION_STORAGE_KEY", "")
    if token == storage_key or token == os.environ.get("API_SERVER_KEY"):
        raise RuntimeError("Hermes connection token must be distinct from other credentials")
    settings = ConnectionSettings(token, storage_key)
    routes = APIServerAdapter._http_route_table
    connect = APIServerAdapter.connect
    select = APIServerAdapter._select_agent_runtime

    def route_table(adapter):
        async def chat(request):
            active = settings.current
            if active.revision and not active.api_key:
                denied = adapter._check_auth(request)
                if denied is not None:
                    return denied
                return web.json_response({"error": {"message": "Hermes connection API key is not configured",
                                                     "type": "configuration_error"}}, status=503)
            return await adapter._handle_chat_completions(request)

        return [
            (method, path, chat if (method, path) == ("POST", "/v1/chat/completions") else handler)
            for method, path, handler in routes(adapter)
        ] + [(method, "/project/connection", settings.handle) for method in ("GET", "PUT")]

    async def connect_with_settings(adapter, **kwargs):
        await settings.refresh()
        return await connect(adapter, **kwargs)

    def select_with_settings(adapter, runtime_kwargs, model, **kwargs):
        active = settings.current
        if not active.revision:
            return select(adapter, runtime_kwargs, model, **kwargs)
        result = select(adapter, runtime_kwargs, active.model, **kwargs)
        if runtime_kwargs.get("provider") == "custom":
            if not active.api_key:
                raise _ProviderAuthResolutionError("Hermes connection API key is not configured")
            # A named provider reloads key_env at turn start and would restore the
            # bootstrap key if the administrator rotates it at the same address.
            runtime_kwargs.update(api_key=active.api_key, base_url=active.base_url,
                                  requested_provider="custom", credential_pool=None)
        return result

    APIServerAdapter._http_route_table = route_table
    APIServerAdapter.connect = connect_with_settings
    APIServerAdapter._select_agent_runtime = select_with_settings
