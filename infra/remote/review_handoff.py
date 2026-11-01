"""Container-only, read-only deployment gate; never starve active renewals.

Run through docker exec stdin with --release-file /tmp/<unique-task-marker>.
The marker must be created only after deployment checks finish.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import time

import asyncpg

from app.config import load_settings
from app.worker import SCHEDULER_LOCK


async def try_hold_idle_scheduler(conn) -> bool:
    acquired = await conn.fetchval('SELECT pg_try_advisory_lock(hashtext($1))', SCHEDULER_LOCK)
    if not acquired:
        return False
    keep = False
    try:
        active = await conn.fetchval(
            "SELECT count(*) FROM v2_analysis_jobs WHERE state='running' AND lease_until>clock_timestamp()")
        keep = active == 0
        return keep
    finally:
        if not keep:
            # Renewals need this same lock. Never wait for active work while
            # holding it, including when the inspection query fails.
            await conn.fetchval('SELECT pg_advisory_unlock(hashtext($1))', SCHEDULER_LOCK)


async def run(release: Path, wait_seconds: float, hold_seconds: float) -> None:
    if not release.is_absolute() or release.parent != Path('/tmp') or release.exists():
        raise ValueError('a new unique /tmp release marker is required')
    conn = await asyncpg.connect(load_settings(role='worker').database_url, timeout=5, command_timeout=5,
        server_settings={'application_name': 'review-idle-deployment-gate',
                         'default_transaction_read_only': 'on', 'statement_timeout': '5000'})
    acquired = False
    try:
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            acquired = await try_hold_idle_scheduler(conn)
            if acquired:
                break
            await asyncio.sleep(2)
        if not acquired:
            raise TimeoutError('no idle handoff window; deployment must not proceed')
        print(json.dumps({'event': 'idle_gate_held', 'at': str(await conn.fetchval('SELECT clock_timestamp()'))}), flush=True)
        deadline = time.monotonic() + hold_seconds
        while time.monotonic() < deadline:
            if release.exists():
                return
            await asyncio.sleep(1)
        raise TimeoutError('deployment gate hold deadline exceeded')
    finally:
        try:
            if acquired:
                await conn.fetchval('SELECT pg_advisory_unlock(hashtext($1))', SCHEDULER_LOCK)
        finally:
            await conn.close(timeout=5)
        print(json.dumps({'event': 'gate_released', 'was_held': acquired}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-file', type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run(args.release_file, 600, 180))
    except Exception as exc:
        print(json.dumps({'event': 'handoff_failed', 'category': type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
