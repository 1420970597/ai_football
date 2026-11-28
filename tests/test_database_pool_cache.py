"""Real temporary PostgreSQL validates bounded caching and schema recovery."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import uuid

import asyncpg

from app import database


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires task-owned temporary PostgreSQL')
class DatabasePoolCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        admin_url = os.environ['TEST_DATABASE_ADMIN_URL']
        parts = urlsplit(admin_url)
        if parts.hostname not in {'ai-football-v2-pgtest', 'ai-football-v2-pgverify'} or parts.path != '/postgres':
            raise RuntimeError('cache tests require task-owned Docker PostgreSQL')
        self.name = 'test_pool_cache_' + uuid.uuid4().hex
        self.admin = await asyncpg.connect(admin_url)
        await self.admin.execute('CREATE DATABASE ' + self.name)
        self.pool_patch = patch.object(database, 'pool', None)
        self.pool_patch.start()
        self.pool = await database.open_pool(SimpleNamespace(
            database_url=admin_url.rsplit('/', 1)[0] + '/' + self.name), verify=False)

    async def asyncTearDown(self):
        await self.pool.close()
        self.pool_patch.stop()
        await self.admin.execute('DROP DATABASE ' + self.name + ' WITH (FORCE)')
        await self.admin.close()

    async def test_cache_remains_bounded_and_json_codecs_survive_capacity_eviction(self):
        async with self.pool.acquire() as conn:
            self.assertEqual(0, conn._config.max_cached_statement_lifetime)
            self.assertEqual(100, conn._config.statement_cache_size)
            self.assertEqual(5, conn._config.command_timeout)
            for index in range(105):
                self.assertEqual(index + 1, await conn.fetchval('SELECT $1::int + ' + str(index), 1))
            self.assertLessEqual(len(conn._stmt_cache), 100)
            self.assertEqual(100, len(conn._stmt_cache))
            payload = {'bounded': True, 'items': [1, 2]}
            self.assertEqual(payload, await conn.fetchval('SELECT $1::jsonb', payload))
            self.assertEqual(payload, await conn.fetchval('SELECT $1::json', payload))
            self.assertLessEqual(len(conn._stmt_cache), 100)

    async def test_cached_statement_recovers_after_result_type_changes(self):
        async with self.pool.acquire() as conn:
            await conn.execute('CREATE TABLE cache_sample(id int PRIMARY KEY, value int)')
            await conn.execute('INSERT INTO cache_sample VALUES(1, 7)')
            query = 'SELECT value FROM cache_sample WHERE id=$1'
            self.assertEqual(7, await conn.fetchval(query, 1))
            await conn.execute('ALTER TABLE cache_sample ALTER COLUMN value TYPE text USING value::text')
            # A stable cached query must refresh its changed result descriptor;
            # disabling wall-clock expiry must not disable schema recovery.
            self.assertEqual('7', await conn.fetchval(query, 1))
            self.assertEqual('7', await conn.fetchval(query, 1))
            self.assertEqual(5, conn._config.command_timeout)
