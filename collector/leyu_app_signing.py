"""Native App 2.0.1 headers, verified against the official Android package.

Keys and IVs belong in a private runtime JSON file, never source control.
The App encrypts a timestamp plus three random digits for X-API-XXX, and
{tt: timestamp_seconds, xx: preInfo.ip} for X-API-HACK-XXXXX.
"""
from __future__ import annotations

import base64
import json
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, Mapping, Optional

from .leyu_client import encrypt_aes_cbc
from .session import SessionError

SIGNING_ENV_CONFIG = "LEYU_APP_SIGNING_CONFIG"
PRE_INFO_PATH = "/site/api/v1/configuration/preInfo"
DEFAULT_CLIENT_IP = "192.168.28.123"  # Official App's preInfo fallback.


class NativeAppSigner:
    def __init__(self, routes: Mapping[str, Mapping[str, str]]) -> None:
        self.routes = dict(routes)
        for route in ("", "/site/api", "/game/api"):
            entry = self.routes.get(route, {})
            if (len(entry.get("key", "").encode()) not in (16, 24, 32)
                    or len(entry.get("iv", "").encode()) != 16):
                raise SessionError("App signing config has invalid key/IV for route %s" % route)
        self._ip = DEFAULT_CLIENT_IP
        self._initialized = False
        self._lock = threading.RLock()

    def _encrypt(self, route: str, text: str) -> bytes:
        entry = self.routes[route]
        return encrypt_aes_cbc(text.encode(), entry["key"].encode(), entry["iv"].encode())

    def headers(self, prefix: str) -> Dict[str, str]:
        with self._lock:
            now = time.time()
            stamp = str(int(now * 1000)) + str(secrets.randbelow(900) + 100)
            binding = json.dumps({"tt": str(int(now)), "xx": self._ip}, separators=(",", ":"))
            return {
                "x-api-xxx": self._encrypt(prefix, stamp).hex(),
                "x-api-hack-xxxxx": base64.b64encode(self._encrypt("", binding)).decode(),
            }

    def initialize(self, host: str, uuid: str, timeout: float) -> None:
        """Load the same preInfo IP as the App; never submit account credentials."""
        with self._lock:
            if self._initialized:
                return
            headers = {
                "x-api-client": "android", "x-api-site": "2001",
                "x-api-version": "2.0.1", "x-api-uuid": uuid, "x-api-token": "",
                "x-api-language": "CHS", "x-api-currency": "CNY",
                "content-type": "application/json; charset=utf-8",
                "user-agent": "okhttp/4.12.0", "accept-encoding": "identity",
                **self.headers("/site/api"),
            }
            req = urllib.request.Request(host.rstrip("/") + PRE_INFO_PATH,
                                         data=b"{}", headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout,
                                            context=ssl._create_unverified_context()) as response:
                    payload = json.load(response)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                raise SessionError("App initialization failed (%s)" % type(exc).__name__) from exc
            if not isinstance(payload, dict) or str(payload.get("status_code")) != "6000":
                raise SessionError("App initialization was not accepted")
            data = payload.get("data")
            if not isinstance(data, dict):
                raise SessionError("App initialization response has no data object")
            self._ip = str(data.get("ip") or DEFAULT_CLIENT_IP)
            self._initialized = True


def signer_from_env(env: Mapping[str, str]) -> Optional[NativeAppSigner]:
    path = env.get(SIGNING_ENV_CONFIG, "").strip()
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as source:
            data = json.load(source)
        routes = data["routes"]
        if not isinstance(routes, dict) or any(not isinstance(v, dict) for v in routes.values()):
            raise ValueError("invalid routes")
        return NativeAppSigner(routes)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise SessionError("Cannot load private App signing config (%s)" % type(exc).__name__) from exc
