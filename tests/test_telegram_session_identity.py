"""Offline stable-account validation for saved Telegram session takeover."""
import asyncio
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from telethon.errors import AuthKeyDuplicatedError

from collector.telegram import TelegramCollector
from test_telegram_collector import Backend, Client, CONFIG


class IdentityClient(Client):
    def __init__(self):
        super().__init__()
        self.identity_calls = 0
        self.identity_failure = None
        self.identity_result = SimpleNamespace(id=1234, first_name='不同昵称', username='renamed')
        self.identity_gate = None

    async def get_me(self):
        self.identity_calls += 1
        if self.identity_gate is not None:
            await self.identity_gate.wait()
        if self.identity_failure is not None:
            raise self.identity_failure
        return self.identity_result


class TelegramSessionIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = IdentityClient()
        self.backend = Backend()
        self.created = []
        def factory(*args):
            self.created.append(args[0])
            return self.client
        self.collector = TelegramCollector(CONFIG, backend=self.backend, client_factory=factory)

    async def asyncTearDown(self):
        await self.collector.disconnect()

    async def test_same_id_ignores_changed_nickname_and_reuses_verified_connection(self):
        original = copy.deepcopy(self.collector.config)
        self.assertTrue(await self.collector.connect())
        self.assertTrue(await self.collector.connect())
        self.assertEqual('1234', self.collector.account['id'])
        self.assertEqual('测试账号', self.collector.account['display_name'])
        self.assertEqual(1, self.client.identity_calls)
        self.assertEqual(4, len(self.client.handlers))
        self.assertEqual(original, self.collector.config)
        self.assertEqual([], self.backend.saved_sessions)

    async def test_wrong_id_even_with_same_name_is_rejected_and_not_retried(self):
        original = copy.deepcopy(self.collector.config)
        self.client.identity_result = SimpleNamespace(id=9999, first_name='测试账号')
        result = await self.collector.sync_once()
        self.assertEqual({'state':'failed','sources':{},'reason':'session_identity_mismatch'}, result)
        self.assertFalse(await self.collector.connect())
        self.assertIsNone(self.collector.client)
        self.assertIsNone(self.collector.account)
        self.assertEqual(1, self.client.identity_calls)
        self.assertEqual(1, len(self.created))
        self.assertFalse(self.client.connected)
        self.assertEqual([], self.client.handlers)
        self.assertEqual([], self.backend.calls)
        self.assertEqual([], self.backend.saved_sessions)
        self.assertEqual(original, self.collector.config)
        self.assertNotIn('9999', repr(self.collector.auth_state))

    async def test_missing_expected_identity_rejects_before_network_and_directory(self):
        self.collector.config.pop('telegram_account')
        result = await self.collector.control('/admins', {'chat_id':'-1001'})
        self.assertEqual('unauthorized', result['state'])
        self.assertEqual('session_identity_expected_missing', result['reason'])
        self.assertEqual([], self.created)
        self.assertEqual(0, self.client.connect_count)
        self.assertEqual(0, self.client.identity_calls)
        self.assertFalse(result['manual_id_allowed'])

    async def test_invalid_expected_ids_never_use_nickname_or_coerce_numbers(self):
        for value in (True, 1234.0, -1234, 0, '001234', '1234.0', '测试账号', 2**63):
            with self.subTest(value=value):
                self.collector.config['telegram_account'] = json.dumps({'id':value})
                self.assertFalse(await self.collector.connect())
                self.assertEqual('session_identity_expected_invalid', self.collector.auth_state['reason'])
        self.assertEqual([], self.created)

    async def test_missing_and_corrupt_expected_record_are_distinct(self):
        for value, reason in ((json.dumps({'display_name':'昵称'}), 'session_identity_expected_missing'),
                              ('invalid-json', 'session_identity_expected_invalid'),
                              ('[]', 'session_identity_expected_invalid')):
            self.collector.config['telegram_account'] = value
            self.assertFalse(await self.collector.connect())
            self.assertEqual(reason, self.collector.auth_state['reason'])

    async def test_unauthorized_session_never_requests_identity(self):
        self.client.authorized = False
        self.assertFalse(await self.collector.connect())
        self.assertEqual('session_invalid', self.collector.auth_state['state'])
        self.assertEqual(0, self.client.identity_calls)
        self.assertEqual([], self.client.handlers)
        self.assertFalse(await self.collector.connect())
        self.assertEqual(1, self.client.connect_count)

    async def test_identity_timeout_disconnects_without_registering_and_can_recover(self):
        self.client.identity_gate = asyncio.Event()
        with patch('collector.telegram.SESSION_IDENTITY_SECONDS', .01):
            self.assertFalse(await asyncio.wait_for(self.collector.connect(), .2))
        self.assertEqual('session_identity_timeout', self.collector.auth_state['reason'])
        self.assertIsNone(self.collector.client)
        self.assertFalse(self.client.connected)
        self.assertEqual([], self.client.handlers)
        self.client.identity_gate = None
        self.assertTrue(await self.collector.connect())
        self.assertEqual(2, self.client.identity_calls)

    async def test_identity_network_failure_is_safe_and_does_not_overwrite_config(self):
        original = copy.deepcopy(self.collector.config)
        self.client.identity_failure = ConnectionError('secret-session-and-account')
        self.assertFalse(await self.collector.connect())
        self.assertEqual({'state':'failed','reason':'session_identity_unavailable'}, self.collector.auth_state)
        self.assertEqual(original, self.collector.config)
        self.assertEqual([], self.backend.saved_sessions)
        self.assertEqual([], self.client.handlers)

    async def test_missing_or_invalid_actual_identity_is_not_accepted(self):
        for value in (None, SimpleNamespace(id=None), SimpleNamespace(id=True), SimpleNamespace(id=1234.0)):
            self.client.identity_result = value
            self.assertFalse(await self.collector.connect())
            self.assertEqual('session_identity_unverifiable', self.collector.auth_state['reason'])
        self.assertEqual([], self.client.handlers)

    async def test_get_me_duplicate_key_uses_existing_invalidation_gate(self):
        self.client.identity_failure = AuthKeyDuplicatedError(None)
        with self.assertRaises(AuthKeyDuplicatedError):
            await self.collector.connect()
        self.assertEqual('session_invalid', self.collector.auth_state['state'])
        self.assertFalse(await self.collector.connect())
        self.assertEqual(1, self.client.identity_calls)
        self.assertFalse(self.client.connected)

    async def test_reconnect_rechecks_stable_identity_before_reuse(self):
        self.assertTrue(await self.collector.connect())
        self.client.connected = False
        self.client.identity_result = SimpleNamespace(id=9999)
        self.assertFalse(await self.collector.connect())
        self.assertEqual('session_identity_mismatch', self.collector.auth_state['reason'])
        self.assertIsNone(self.collector.client)
        self.assertEqual(2, self.client.identity_calls)

    async def test_expected_identity_change_releases_rejection_only_after_config_change(self):
        self.client.identity_result = SimpleNamespace(id=9999)
        self.assertFalse(await self.collector.connect())
        replacement = CONFIG | {'telegram_account':json.dumps({'id':'9999','display_name':'已配置账号'})}
        await self.collector.apply_config(replacement)
        self.assertTrue(await self.collector.connect())
        self.assertEqual('9999', self.collector.account['id'])
        self.assertEqual([], self.backend.saved_sessions)

    async def test_nickname_only_config_change_does_not_reconnect_or_renegotiate_identity(self):
        self.assertTrue(await self.collector.connect())
        await self.collector.apply_config(CONFIG | {'telegram_account':json.dumps({'id':'1234','display_name':'新昵称'})})
        self.assertTrue(await self.collector.connect())
        self.assertEqual(1, self.client.identity_calls)
        self.assertEqual(1, self.client.connect_count)
        self.assertEqual('新昵称', self.collector.account['display_name'])

    async def test_expected_id_change_disconnects_verified_client_before_readoption(self):
        self.assertTrue(await self.collector.connect())
        await self.collector.apply_config(CONFIG | {'telegram_account':json.dumps({'id':'9999'})})
        self.assertIsNone(self.collector.client)
        self.assertIsNone(self.collector.account)
        self.assertFalse(self.client.connected)
        self.assertFalse(await self.collector.connect())
        self.assertEqual('session_identity_mismatch', self.collector.auth_state['reason'])
        self.assertEqual(2, self.client.identity_calls)

    async def test_configuration_change_waits_until_takeover_finishes_then_discards_old_identity(self):
        self.client.identity_gate = asyncio.Event()
        first = asyncio.create_task(self.collector.connect())
        while self.client.identity_calls == 0:
            await asyncio.sleep(0)
        update = asyncio.create_task(self.collector.apply_config(
            CONFIG | {'telegram_session':'new-test-session', 'telegram_account':json.dumps({'id':'9999'})}))
        await asyncio.sleep(0)
        self.assertFalse(update.done())
        self.client.identity_gate.set()
        self.assertTrue(await first)
        await update
        self.assertIsNone(self.collector.client)
        self.assertIsNone(self.collector.account)
        self.assertFalse(self.client.connected)
        self.assertEqual('new-test-session', self.collector.config['telegram_session'])

    async def test_concurrent_takeovers_create_one_verified_client_and_handler_set(self):
        self.client.identity_gate = asyncio.Event()
        first = asyncio.create_task(self.collector.connect())
        await asyncio.sleep(0)
        second = asyncio.create_task(self.collector.connect())
        self.client.identity_gate.set()
        self.assertEqual([True,True], await asyncio.gather(first,second))
        self.assertEqual(1, self.client.identity_calls)
        self.assertEqual(1, len(self.created))
        self.assertEqual(4, len(self.client.handlers))

    async def test_cancelled_takeover_disconnects_and_never_installs_handlers(self):
        self.client.identity_gate = asyncio.Event()
        task = asyncio.create_task(self.collector.connect())
        while self.client.identity_calls == 0:
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.client.connected)
        self.assertIsNone(self.collector.client)
        self.assertEqual([], self.client.handlers)
