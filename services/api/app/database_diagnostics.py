"""Local-only, bounded identifiers for correlating failures with PG activity."""

from __future__ import annotations

import asyncpg
import json
import math


def exception_backend_pids(exc: BaseException) -> str:
    """Read connection metadata, never queries, parameters or exception text."""
    pids: list[int] = []
    traceback = exc.__traceback__
    for _ in range(64):
        if traceback is None:
            break
        connection = traceback.tb_frame.f_locals.get("self")
        # Only the configured driver's concrete connection is accepted.  Do
        # not invoke a lookalike object's method from arbitrary application frames.
        if type(connection) is asyncpg.Connection:
            try:
                pid = asyncpg.Connection.get_server_pid(connection)
            except Exception:
                pid = None
            if type(pid) is int and 0 < pid < 2**31 and pid not in pids:
                pids.append(pid)
                if len(pids) == 8:
                    break
        traceback = traceback.tb_next
    return ",".join(map(str, pids)) or "none"


def exception_timeout_budget(exc: BaseException) -> str:
    """Read only numeric budgets from the pinned driver's own execution frames."""
    if not isinstance(exc, TimeoutError):
        return "none"
    try:
        parents: dict[int, float] = {}
        results: list[dict] = []
        traceback = exc.__traceback__
        for _ in range(64):
            if traceback is None:
                break
            frame = traceback.tb_frame
            local = frame.f_locals
            connection = local.get("self")
            if type(connection) is asyncpg.Connection:
                code = frame.f_code
                timeout = local.get("timeout")
                valid_timeout = type(timeout) in (int, float) and math.isfinite(timeout)
                if code is asyncpg.Connection._Connection__execute.__code__:
                    if valid_timeout and 0 < timeout <= 3600:
                        parents[id(connection)] = timeout
                elif (code is asyncpg.Connection._do_execute.__code__
                      and local.get("stmt") is not None and local.get("retry") is True):
                    initial = parents.get(id(connection))
                    before, after = local.get("before"), local.get("after")
                    if (initial is not None and valid_timeout
                        and type(before) in (int, float) and type(after) in (int, float)
                        and math.isfinite(before) and math.isfinite(after) and after >= before):
                        execution = after - before
                        preparation = initial - timeout - execution
                        if -0.001 <= preparation <= 3600 and execution <= 3600 and abs(timeout) <= 3600:
                            preparation = max(0, preparation)
                            results.append({
                                "phase": "preparation_exhausted" if preparation >= initial else "execution_wait",
                                "budget_ms": round(initial * 1000, 3),
                                "prepare_ms": round(preparation * 1000, 3),
                                "execute_ms": round(execution * 1000, 3),
                                "remaining_ms": round(timeout * 1000, 3),
                            })
                            if len(results) == 4:
                                break
            traceback = traceback.tb_next
        return json.dumps(results, separators=(",", ":"), allow_nan=False) if results else "none"
    except Exception:
        # Diagnostics must neither replace the original exception nor inspect its text.
        return "none"
