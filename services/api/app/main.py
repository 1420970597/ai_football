from __future__ import annotations

import logging
import os
import re
import uuid
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import admin_mfa, auth, database, identity_lifecycle
from .config import load_settings
from .health import HermesProbe, MCPProbe, StorageProbe
from .routes import accounts, admin, admin_reviews, expert_content, internal, operations, public, renewal, results, reviews, telegram
from .security import decrypt_secret

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("platform.api")


_SAFE_FRAME = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_.]*|<[A-Za-z0-9_]+>):\d+$")


def _safe_exception_frames(exc: BaseException, *, limit: int = 8) -> str:
    """Return only code locations; never serialize exception text or locals.

    Exception strings may contain a PostgreSQL DSN, an upstream token, or a
    request body.  The global handler is deliberately limited to a bounded
    list of traceback code locations so diagnostics remain useful without
    copying those values into the log sink.
    """
    frames = []
    traceback = exc.__traceback__
    while traceback is not None:
        code_name = traceback.tb_frame.f_code.co_name
        candidate = f"{code_name}:{traceback.tb_lineno}"
        if _SAFE_FRAME.fullmatch(candidate):
            frames.append(candidate)
        traceback = traceback.tb_next
    return ",".join(frames[-limit:])


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


@app.middleware("http")
async def request_context(request: Request, call_next):
    supplied = request.headers.get("x-request-id", "")
    request_id = supplied if len(supplied) <= 64 and supplied and all(c.isalnum() or c in "-_." for c in supplied) else str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = "no-store" if response.status_code >= 400 or request.url.path.startswith(("/api/", "/internal/")) else "public, max-age=60"
    return response


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    log.error("request failure request_id=%s category=%s frames=%s", getattr(request.state, "request_id", ""), type(exc).__name__, _safe_exception_frames(exc))
    return JSONResponse(
        status_code=500,
        content={"detail": "服务暂不可用", "request_id": getattr(request.state, "request_id", "")},
    )


async def database_temporarily_unavailable(request: Request, exc: Exception):
    """Expose transient contention without replaying an unconfirmed write.

    A timeout during COMMIT can leave the outcome unknown.  This response
    describes service availability, not a rollback or permission to repeat a
    non-idempotent operation.  Callers must reconcile such writes first.
    """
    request_id = getattr(request.state, "request_id", "")
    log.warning("database temporarily unavailable request_id=%s category=%s frames=%s",
                request_id, type(exc).__name__, _safe_exception_frames(exc))
    content = {"detail": "数据库暂时繁忙，请稍后重试", "code": "database_temporarily_unavailable",
               "request_id": request_id, "retryable": True, "automatic_retry": False}
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        content["operation_status"] = "unconfirmed"
        content["detail"] = "数据库暂时繁忙，写入结果未确认；请先核对结果再重试"
    return JSONResponse(status_code=503, content=content, headers={"Retry-After": "2", "Cache-Control": "no-store"})


@app.exception_handler(TimeoutError)
async def timeout_error(request: Request, exc: TimeoutError):
    # asyncpg command/pool timeouts use the built-in TimeoutError.  Unrelated
    # timeouts remain unexpected errors rather than being mislabeled as DB busy.
    traceback = exc.__traceback__
    while traceback is not None:
        module = traceback.tb_frame.f_globals.get("__name__", "")
        if isinstance(module, str) and (module == "asyncpg" or module.startswith("asyncpg.")):
            return await database_temporarily_unavailable(request, exc)
        traceback = traceback.tb_next
    return await unexpected_error(request, exc)


for transient_database_error in (
    asyncpg.LockNotAvailableError, asyncpg.QueryCanceledError, asyncpg.DeadlockDetectedError,
    asyncpg.SerializationError, asyncpg.TooManyConnectionsError, asyncpg.CannotConnectNowError,
    asyncpg.PostgresConnectionError,
):
    app.add_exception_handler(transient_database_error, database_temporarily_unavailable)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "version": "2.0.0"}


@app.get("/readyz")
async def readyz():
    await database.current_pool().fetchval("SELECT 1")
    return {"status": "ready"}


app.include_router(auth.router)
app.include_router(identity_lifecycle.router)
app.include_router(admin_mfa.router)
app.include_router(public.router)
app.include_router(admin.router)
app.include_router(operations.router)
app.include_router(admin_reviews.router)
app.include_router(accounts.router)
app.include_router(accounts.executor_router)
app.include_router(internal.router)
app.include_router(reviews.router)
app.include_router(renewal.router)
app.include_router(results.router)
app.include_router(telegram.router)
app.include_router(expert_content.router)
