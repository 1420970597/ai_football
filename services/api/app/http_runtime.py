"""Shared HTTP diagnostics and error handling for API and limited tools."""
import logging
import re
import time
import uuid

import asyncpg
from fastapi import Request
from fastapi.responses import JSONResponse

from .database_diagnostics import exception_backend_pids, exception_timeout_budget

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


async def request_context(request: Request, call_next):
    supplied = request.headers.get("x-request-id", "")
    request_id = supplied if len(supplied) <= 64 and supplied and all(c.isalnum() or c in "-_." for c in supplied) else str(uuid.uuid4())
    request.state.request_id = request_id
    operation = {
        "/internal/telegram/events": "events",
        "/internal/telegram/events/receipts": "receipts",
    }.get(request.url.path)
    started = time.perf_counter()
    status = "no_response"
    category = "none"
    try:
        response = await call_next(request)
        status = str(response.status_code)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        existing_cache = {part.strip().lower() for part in response.headers.get("Cache-Control", "").split(",")}
        if request.url.path.startswith("/api/") or "private" in existing_cache:
            # Message evidence and authenticated errors must remain private; a
            # generic middleware default must not erase the route's stronger rule.
            response.headers["Cache-Control"] = "private, no-store"
        elif response.status_code >= 400 or request.url.path.startswith("/internal/") or "no-store" in existing_cache:
            response.headers["Cache-Control"] = "no-store"
        elif not response.headers.get("Cache-Control"):
            response.headers["Cache-Control"] = "public, max-age=60"
        return response
    except Exception as exc:
        category = type(exc).__name__
        raise
    finally:
        if operation is not None:
            phases = getattr(request.state, "telegram_event_phases", {})
            phase_values = {key: float(value) for key, value in phases.items()
                            if isinstance(key, str) and key.endswith("_ms")
                            and isinstance(value, (int, float))}
            elapsed_ms = (time.perf_counter() - started) * 1000
            if phase_values:
                accounted_phases = getattr(request.state, "telegram_event_accounted_phases", None)
                accounted_ms = (sum(phase_values.get(name, 0.0) for name in accounted_phases)
                                if accounted_phases is not None else sum(phase_values.values()))
                phase_values["remaining_ms"] = round(max(0.0, elapsed_ms - accounted_ms), 1)
                phase_summary = ",".join(f"{key}={value:.1f}" for key, value in sorted(phase_values.items()))
            else:
                phase_summary = "none"
            event_path = getattr(request.state, "telegram_event_path", "unclassified")
            log.info("Telegram API request method=%s operation=%s status=%s category=%s elapsed_ms=%.1f event_path=%s phases=%s",
                     request.method, operation, status, category, elapsed_ms, event_path, phase_summary or "none")


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
    log.warning("database temporarily unavailable request_id=%s category=%s frames=%s driver_budget=%s backend_pids=%s",
                request_id, type(exc).__name__, _safe_exception_frames(exc),
                exception_timeout_budget(exc), exception_backend_pids(exc))
    content = {"detail": "数据库暂时繁忙，请稍后重试", "code": "database_temporarily_unavailable",
               "request_id": request_id, "retryable": True, "automatic_retry": False}
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        content["operation_status"] = "unconfirmed"
        content["detail"] = "数据库暂时繁忙，写入结果未确认；请先核对结果再重试"
    return JSONResponse(status_code=503, content=content, headers={"Retry-After": "2", "Cache-Control": "no-store"})


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


async def database_interface_error(request: Request, exc: asyncpg.InterfaceError):
    # A disconnect during a transaction can be masked by InterfaceError when
    # __aexit__ discovers its connection is already closed.  Classify only an
    # explicit typed connection failure in the bounded chain, never messages
    # such as "connection closed" or arbitrary driver programming errors.
    pending, seen = [exc], set()
    for _ in range(8):
        if not pending:
            break
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (asyncpg.PostgresConnectionError, asyncpg.CannotConnectNowError)):
            return await database_temporarily_unavailable(request, exc)
        for linked in (current.__context__, current.__cause__):
            if linked is not None and id(linked) not in seen:
                pending.append(linked)
    return await unexpected_error(request, exc)


def register_error_handlers(app):
    app.add_exception_handler(Exception, unexpected_error)
    app.add_exception_handler(TimeoutError, timeout_error)
    app.add_exception_handler(asyncpg.InterfaceError, database_interface_error)
    for transient_database_error in (
        asyncpg.LockNotAvailableError, asyncpg.QueryCanceledError, asyncpg.DeadlockDetectedError,
        asyncpg.SerializationError, asyncpg.TooManyConnectionsError, asyncpg.CannotConnectNowError,
        asyncpg.PostgresConnectionError,
    ):
        app.add_exception_handler(transient_database_error, database_temporarily_unavailable)
