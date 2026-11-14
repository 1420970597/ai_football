"""Account fixture cleanup must not require cross-role process privileges."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import asyncpg

import test_account_state as fixture


class AccountFixtureCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def check_cleanup(self, failure=None):
        events = []
        original_pool = fixture.database.pool
        previous_pool = object()
        admin = SimpleNamespace(
            execute=AsyncMock(side_effect=failure), close=AsyncMock())

        async def close_pool():
            events.append('pool_closed')

        async def connect(dsn):
            self.assertEqual(['pool_closed'], events)
            self.assertIs(fixture.database.pool, previous_pool)
            return admin

        state = SimpleNamespace(
            patch=SimpleNamespace(stop=Mock()),
            api=SimpleNamespace(aclose=AsyncMock()),
            worker_api=SimpleNamespace(aclose=AsyncMock()),
            old_pool=previous_pool, pool=SimpleNamespace(close=close_pool),
            dbname='account_state_cleanup_test')
        try:
            with patch.object(fixture.asyncpg, 'connect', side_effect=connect):
                if failure is None:
                    await fixture.AccountStateTests.asyncTearDown(state)
                else:
                    with self.assertRaises(type(failure)) as caught:
                        await fixture.AccountStateTests.asyncTearDown(state)
                    self.assertIs(caught.exception, failure)
            admin.execute.assert_awaited_once_with(
                'DROP DATABASE account_state_cleanup_test')
            admin.close.assert_awaited_once_with()
            state.api.aclose.assert_awaited_once_with()
            state.worker_api.aclose.assert_awaited_once_with()
            state.patch.stop.assert_called_once_with()
        finally:
            fixture.database.pool = original_pool

    async def test_pool_closes_before_unforced_drop(self):
        await self.check_cleanup()

    async def test_leaked_connection_failure_is_not_suppressed(self):
        await self.check_cleanup(asyncpg.ObjectInUseError('isolated active client'))

    async def test_permission_failure_is_not_suppressed(self):
        await self.check_cleanup(asyncpg.InsufficientPrivilegeError('isolated permission'))
