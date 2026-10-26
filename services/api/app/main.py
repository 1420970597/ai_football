from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import auth, database
from .config import load_settings
from .health import HermesProbe
from .routes import accounts, admin, expert_content, internal, public, renewal, results, reviews, telegram
from .security import decrypt_secret

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("platform.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.settings = settings
    app.state.hermes_probe = HermesProbe(settings.hermes_api_url)
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
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "public, max-age=60"
    return response


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    frames = []
    tb = exc.__traceback__
    while tb is not None:
        frames.append(f"{tb.tb_frame.f_code.co_name}:{tb.tb_lineno}")
        tb = tb.tb_next
    log.error("request failure request_id=%s category=%s frames=%s", getattr(request.state, "request_id", ""), type(exc).__name__, ",".join(frames[-8:]))
    return JSONResponse(
        status_code=500,
        content={"detail": "服务暂不可用", "request_id": getattr(request.state, "request_id", "")},
    )


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "version": "2.0.0"}


@app.get("/readyz")
async def readyz():
    await database.current_pool().fetchval("SELECT 1")
    return {"status": "ready"}


app.include_router(auth.router)
app.include_router(public.router)
app.include_router(admin.router)
app.include_router(accounts.router)
app.include_router(accounts.executor_router)
app.include_router(internal.router)
app.include_router(reviews.router)
app.include_router(renewal.router)
app.include_router(results.router)
app.include_router(telegram.router)
app.include_router(expert_content.router)
