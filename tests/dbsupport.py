"""Per-test PostgreSQL databases cloned from one migrated template.

Applying every migration to a fresh database costs about a second per test
case; cloning an already migrated template is a single statement. Each test
still owns a private database and drops it itself.

The template name carries a digest of the migration set and the creating
role, so a changed migration or a different owner never reuses a stale
template. Templates are built under a unique name and published by rename, so
concurrent processes on one server cannot observe a half-migrated template.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlsplit, urlunsplit
import uuid

import asyncpg

from app.database import apply_migrations, migration_files

TEST_HOSTS = frozenset({'pgtest', 'ai-football-v2-pgtest', 'ai-football-v2-pgverify'})
_templates: dict[str, str] = {}


async def create_database(admin, admin_url: str, name: str) -> None:
    """Create database ``name`` with all migrations applied.

    ``admin`` is an open connection made from ``admin_url``; the caller keeps
    ownership of both the connection and the new database.
    """
    template = _templates.get(admin_url)
    if template is None:
        template = _templates[admin_url] = await _publish_template(admin, admin_url)
    await admin.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template}"')


async def _publish_template(admin, admin_url: str) -> str:
    parts = urlsplit(admin_url)
    if parts.hostname not in TEST_HOSTS:
        raise RuntimeError('test databases are only created on the task-owned Docker PostgreSQL')
    digest = hashlib.sha256(
        ''.join(version + checksum for version, checksum, _ in migration_files()).encode()).hexdigest()[:16]
    template = f'test_template_{digest}_{await admin.fetchval("SELECT current_user")}'
    if await admin.fetchval('SELECT 1 FROM pg_database WHERE datname=$1', template):
        return template
    building = 'test_template_build_' + uuid.uuid4().hex
    await admin.execute(f'CREATE DATABASE "{building}"')
    try:
        conn = await asyncpg.connect(urlunsplit((parts.scheme, parts.netloc, '/' + building, parts.query, '')))
        try:
            await apply_migrations(conn)
        finally:
            await conn.close()
        # A connected session would block every later clone of the template.
        await admin.execute(f'ALTER DATABASE "{building}" ALLOW_CONNECTIONS false')
        await admin.execute(f'ALTER DATABASE "{building}" RENAME TO "{template}"')
    except BaseException as error:
        await admin.execute(f'DROP DATABASE IF EXISTS "{building}"')
        # Losing the publish race to another process leaves its template usable.
        if not isinstance(error, asyncpg.DuplicateDatabaseError):
            raise
    return template
