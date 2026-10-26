from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from urllib.parse import quote


def _clean(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return value


def _required(name: str) -> str:
    value = _clean(os.environ.get(name, ""))
    if not value:
        raise RuntimeError(f"required configuration is missing: {name}")
    return value


def database_url() -> str:
    direct = _clean(os.environ.get("DATABASE_URL", ""))
    if direct:
        return direct
    host = _clean(os.environ.get("PG_BASE_URL", "")).strip("/")
    port = _clean(os.environ.get("PG_BASE_PORT", ""))
    user = _clean(os.environ.get("PG_USER", ""))
    password = _clean(os.environ.get("PG_PASSWORD", ""))
    database = _clean(os.environ.get("PG_DATASET", ""))
    if not all((host, port, user, password, database)):
        raise RuntimeError("set DATABASE_URL or all remote PG_BASE_* connection settings")
    return f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{quote(database, safe='')}?sslmode=require"


@dataclass(frozen=True)
class Settings:
    database_url: str
    session_secret: bytes
    encryption_key: bytes
    hermes_api_url: str
    hermes_api_key: str
    hermes_model: str
    mcp_internal_token: str
    collector_token: str
    quote_max_age_seconds: int
    cookie_secure: bool
    bootstrap_admin_email: str
    bootstrap_admin_password: str
    bootstrap_admin_totp_secret: str
    executor_token: str = ""
    live_orders_enabled: bool = False


def _secret_bytes(name: str, expected: int) -> bytes:
    raw = _required(name)
    try:
        value = bytes.fromhex(raw)
    except ValueError:
        try:
            value = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        except Exception as exc:
            raise RuntimeError(f"{name} must be hex or URL-safe base64") from exc
    if len(value) != expected:
        raise RuntimeError(f"{name} must decode to {expected} bytes")
    return value


def load_settings(*, role: str = "api") -> Settings:
    if role not in {"api", "worker"}:
        raise ValueError("unknown service configuration role")
    return Settings(
        database_url=database_url(),
        session_secret=_secret_bytes("APP_SESSION_SECRET", 32) if role == "api" else b"",
        encryption_key=_secret_bytes("DATA_ENCRYPTION_KEY", 32) if role == "api" else b"",
        hermes_api_url=_clean(os.environ.get("HERMES_API_URL", "http://hermes:8642/v1")).rstrip("/"),
        hermes_api_key=_required("HERMES_API_KEY") if role == "worker" else "",
        hermes_model=_clean(os.environ.get("HERMES_API_MODEL", "hermes-agent")),
        mcp_internal_token=_required("EXPERT_MCP_TOKEN") if role == "api" else "",
        collector_token=_required("COLLECTOR_INTERNAL_TOKEN") if role == "api" else "",
        quote_max_age_seconds=max(1, int(_clean(os.environ.get("QUOTE_MAX_AGE_SECONDS", "15")))),
        cookie_secure=_clean(os.environ.get("COOKIE_SECURE", "true")).lower() == "true",
        bootstrap_admin_email=_clean(os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "")).lower(),
        bootstrap_admin_password=_clean(os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "")),
        bootstrap_admin_totp_secret=_clean(os.environ.get("BOOTSTRAP_ADMIN_TOTP_SECRET", "")).replace(" ", "").upper(),
        executor_token=_clean(os.environ.get("EXECUTOR_INTERNAL_TOKEN", "")) if role == "api" else "",
        live_orders_enabled=role == "api" and _clean(os.environ.get("LIVE_ORDERS_ENABLED", "false")).lower() == "true",
    )
