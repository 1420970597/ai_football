from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from . import auth, database, identity_lifecycle
from .config import load_settings
from .http_runtime import (_safe_exception_frames, request_context, unexpected_error,
                           database_temporarily_unavailable, timeout_error,
                           database_interface_error, register_error_handlers)
from .health import HermesProbe, MCPProbe, StorageProbe
from .routes import accounts, admin, admin_reviews, expert_content, internal, operations, public, renewal, result_checks, results, reviews, telegram
from .security import decrypt_secret
from .routes import account_history

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("platform.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.settings = settings
    app.state.hermes_probe = HermesProbe(settings.hermes_api_url)
    app.state.storage_probe = StorageProbe()
    app.state.mcp_probe = MCPProbe(os.environ.get("MCP_HEALTH_URL", "http://hermes-mcp:8090/mcp"), settings.mcp_internal_token)
    app.state.decrypt_secret = lambda value: decrypt_secret(settings.encryption_key, value)
    pool = await database.open_pool(settings)
    try:
        await auth.bootstrap_admin(pool, settings)
        yield
    finally:
        await database.close_pool()


app = FastAPI(
    title="Football Expert Platform API",
    version="2.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


app.middleware("http")(request_context)
register_error_handlers(app)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "version": "2.0.0"}


@app.get("/readyz")
async def readyz():
    await database.current_pool().fetchval("SELECT 1")
    return {"status": "ready"}


app.include_router(auth.router)
app.include_router(identity_lifecycle.router)
app.include_router(public.router)
app.include_router(admin.router)
app.include_router(operations.router)
app.include_router(admin_reviews.router)
app.include_router(accounts.router)
app.include_router(accounts.executor_router)
app.include_router(account_history.router)
app.include_router(account_history.executor_router)
app.include_router(internal.router)
app.include_router(reviews.router)
app.include_router(renewal.router)
app.include_router(results.router)
app.include_router(result_checks.router)
app.include_router(telegram.router)
app.include_router(expert_content.router)
