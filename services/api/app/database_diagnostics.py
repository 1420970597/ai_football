"""Local-only, bounded identifiers for correlating failures with PG activity."""

from __future__ import annotations

import asyncpg


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
