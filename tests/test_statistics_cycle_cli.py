"""Disposable migration compatibility and operational reset command checks."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import urlsplit
import uuid

import asyncpg

from app.database import MIGRATIONS, apply_migrations


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'), 'requires owned isolated Docker PostgreSQL')
class StatisticsCycleCLITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        admin_url = os.environ['TEST_DATABASE_ADMIN_URL']
        if urlsplit(admin_url).hostname != 'ai-football-v2-pgverify':
            raise RuntimeError('statistics cycle test only permits owned Docker database alias')
        self.admin = await asyncpg.connect(admin_url)
        self.name = 'test_profitable_cli_' + uuid.uuid4().hex
        await self.admin.execute('CREATE DATABASE ' + self.name)
        self.dsn = admin_url.rsplit('/', 1)[0] + '/' + self.name
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=2)
        self.directory = tempfile.TemporaryDirectory(prefix='statistics-cycle-test-')

    async def asyncTearDown(self):
        await self.pool.close()
        await self.admin.execute('DROP DATABASE ' + self.name)
        await self.admin.close()
        self.directory.cleanup()

    async def command(self, *args):
        process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'app.statistics_cycle', *args,
            env={**os.environ, 'DATABASE_URL':self.dsn}, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
        self.assertEqual(b'', stderr)
        return process.returncode, json.loads(stdout)

    async def test_preview_private_manifest_apply_replay_and_revoke(self):
        async with self.pool.acquire() as conn:
            await apply_migrations(conn)
        manifest = Path(self.directory.name) / 'reviewed-plan.json'
        code, plan = await self.command('preview', '--output', str(manifest))
        self.assertEqual(0, code)
        self.assertEqual(0, plan['expert_count'])
        self.assertEqual(0o600, manifest.stat().st_mode & 0o777)
        original = manifest.read_bytes()
        self.assertEqual(1, (await self.command('preview', '--output', str(manifest)))[0])
        self.assertEqual(original, manifest.read_bytes())
        args = ('apply', '--plan', str(manifest), '--key', 'isolated-cli', '--reason', 'isolated no-funds verification')
        code, result = await self.command(*args)
        self.assertEqual(0, code)
        self.assertFalse(result['idempotent'])
        self.assertTrue((await self.command(*args))[1]['idempotent'])
        self.assertEqual(1, await self.pool.fetchval('SELECT count(*) FROM v2_expert_statistics_resets'))
        self.assertEqual(0, await self.pool.fetchval('SELECT count(*) FROM v2_bet_orders'))
        code, revoked = await self.command('revoke', '--reset-id', result['reset_id'], '--reason', 'isolated restore')
        self.assertEqual(0, code)
        self.assertFalse(revoked['idempotent'])
        self.assertEqual(2, await self.pool.fetchval("SELECT count(*) FROM v2_audit_log WHERE resource_type='expert_statistics_cycle'"))

    async def test_incremental_migration_keeps_existing_owner_policy_manual_and_unchanged(self):
        previous = Path(self.directory.name) / 'previous-migrations'
        previous.mkdir()
        for item in MIGRATIONS.glob('[0-9]*_*.sql'):
            if not item.name.startswith('030_'):
                (previous/item.name).write_bytes(item.read_bytes())
        async with self.pool.acquire() as conn:
            await apply_migrations(conn, previous)
        user = uuid.uuid4()
        expert = uuid.uuid4()
        await self.pool.execute("INSERT INTO v2_users(id,email,display_name,password_hash,role) VALUES($1,$2,'isolated','hash','user')", user, str(user)+'@test.invalid')
        await self.pool.execute('INSERT INTO v2_user_bet_settings(user_id,auto_enabled,emergency_stopped,allowed_experts,stake_per_order,max_per_order,daily_limit,open_exposure_limit) '
                               'VALUES($1,TRUE,FALSE,$2,2,3,10,5)', user, [expert])
        before = dict(await self.pool.fetchrow('SELECT * FROM v2_user_bet_settings WHERE user_id=$1', user))
        async with self.pool.acquire() as conn:
            self.assertEqual(['030_profitable_expert_mode.sql'], await apply_migrations(conn))
            self.assertEqual([], await apply_migrations(conn))
        after = dict(await self.pool.fetchrow('SELECT * FROM v2_user_bet_settings WHERE user_id=$1', user))
        self.assertEqual('manual', after.pop('expert_selection_mode'))
        self.assertEqual(before, after)
