from __future__ import annotations

import logging
import json
import hashlib
from pathlib import Path

import asyncpg

from .config import Settings

log = logging.getLogger(__name__)
pool: asyncpg.Pool | None = None
MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
MIGRATION_LOCK = "ai-football-platform-v2-schema"


class MigrationRequired(RuntimeError):
    """数据库结构未升级时阻止普通服务启动，而不是偷偷执行 DDL。"""


def migration_files(directory: Path = MIGRATIONS) -> list[tuple[str, str, str]]:
    result = []
    for path in sorted(directory.glob("[0-9]*_*.sql")):
        content = path.read_text(encoding="utf-8")
        result.append((path.name, hashlib.sha256(content.encode()).hexdigest(), content))
    if not result:
        raise MigrationRequired("no database migration definitions")
    return result


async def check_schema(conn, directory: Path = MIGRATIONS) -> None:
    exists = await conn.fetchval("SELECT to_regclass('public.v2_schema_migrations') IS NOT NULL")
    if not exists:
        raise MigrationRequired("database migration required; run the migration service")
    rows = await conn.fetch("SELECT version,checksum FROM v2_schema_migrations")
    actual = {row["version"]: row["checksum"] for row in rows}
    expected = {version: checksum for version, checksum, _ in migration_files(directory)}
    if actual != expected:
        raise MigrationRequired("database schema version or checksum differs from this release")


async def apply_migrations(conn, directory: Path = MIGRATIONS) -> list[str]:
    """受控迁移入口。整个批次事务化，锁与版本登记同属该事务。"""
    definitions = migration_files(directory)
    applied = []
    async with conn.transaction():
        await conn.execute("SELECT set_config('lock_timeout','30000',true)")
        await conn.execute("SELECT set_config('statement_timeout','60000',true)")
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", MIGRATION_LOCK)
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS v2_schema_migrations ("
            "version TEXT PRIMARY KEY,checksum TEXT NOT NULL,applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        rows = await conn.fetch("SELECT version,checksum FROM v2_schema_migrations")
        existing = {row["version"]: row["checksum"] for row in rows}
        known = {version for version, _, _ in definitions}
        if set(existing) - known:
            raise MigrationRequired("database contains a newer or unknown migration")
        for version, checksum, content in definitions:
            if version in existing:
                if existing[version] != checksum:
                    raise MigrationRequired("applied migration checksum changed")
                continue
            await conn.execute(content)
            await conn.execute(
                "INSERT INTO v2_schema_migrations(version,checksum) VALUES($1,$2)", version, checksum,
            )
            applied.append(version)
    return applied


async def open_pool(settings: Settings, *, migrate: bool = False, verify: bool = True) -> asyncpg.Pool:
    global pool

    async def init_connection(conn) -> None:
        for type_name in ("json", "jsonb"):
            await conn.set_type_codec(
                type_name, schema="pg_catalog", encoder=json.dumps, decoder=json.loads,
            )

    candidate = await asyncpg.create_pool(
        settings.database_url,
        min_size=2,
        max_size=24,
        command_timeout=5,
        max_inactive_connection_lifetime=180,
        server_settings={
            "application_name": "ai-football-platform-v2",
            "statement_timeout": "5000",
            "lock_timeout": "2000",
            "idle_in_transaction_session_timeout": "10000",
        },
        init=init_connection,
    )
    try:
        async with candidate.acquire() as conn:
            if migrate:
                await apply_migrations(conn)
            if verify:
                await check_schema(conn)
    except BaseException:
        await candidate.close()
        raise
    pool = candidate
    log.info("PostgreSQL ready; release schema verified")
    return pool


async def close_pool() -> None:
    global pool
    if pool:
        await pool.close()
        pool = None


def current_pool() -> asyncpg.Pool:
    if pool is None:
        raise RuntimeError("database pool is not ready")
    return pool
