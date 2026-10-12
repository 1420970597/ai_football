import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from api.app import ApiApp
from service.account_summary import AccountSummary


class AccountSummaryTests(unittest.TestCase):
    def test_slow_upstream_never_blocks_api_and_concurrent_reads_share_refresh(self):
        entered, release = threading.Event(), threading.Event()
        client = MagicMock()
        def fetch():
            entered.set()
            release.wait(5)
            return {'available': True, 'sports_balance': 10}
        client.fetch.side_effect = fetch
        factory = MagicMock(return_value=client)
        summary = AccountSummary(factory)
        app = ApiApp(MagicMock())
        app._account_summary = summary
        try:
            first = app.h_account({}, {})
            self.assertTrue(first['loading'])
            self.assertTrue(entered.wait(2))
            with ThreadPoolExecutor(max_workers=4) as pool:
                values = list(pool.map(lambda _: app.h_account({}, {}), range(20)))
            self.assertTrue(all(v['loading'] for v in values))
            client.fetch.assert_called_once()
            factory.assert_called_once()
        finally:
            release.set()
            summary._thread.join(5)
        self.assertEqual(summary.read()['sports_balance'], 10)
        self.assertFalse(summary.read()['loading'])

    def test_refresh_failure_keeps_previous_value_and_reports_staleness(self):
        client = MagicMock()
        client.fetch.return_value = {'available': True, 'sports_balance': 10}
        summary = AccountSummary(lambda: client)
        summary.read()
        summary._thread.join(5)
        timestamp = summary.read()['updated_at_ms']
        client.fetch.side_effect = TimeoutError('SECRET must never be exposed')
        with patch('service.account_summary.ACCOUNT_REFRESH_S', 0):
            summary.read()
            summary._thread.join(5)
            value = summary.read()
            summary._thread.join(5)
        self.assertEqual(value['sports_balance'], 10)
        self.assertEqual(value['updated_at_ms'], timestamp)
        self.assertTrue(value['stale'])
        self.assertIn('TimeoutError', value['refresh_error'])
        self.assertNotIn('SECRET', str(value))

    def test_changed_credentials_never_return_previous_account_data(self):
        client = MagicMock()
        client.fetch.return_value = {'available': True, 'sports_balance': 10}
        summary = AccountSummary(lambda: client)
        with patch.dict('os.environ', {'LEYU_APP_LOGIN_NAME': 'first'}):
            summary.read()
            summary._thread.join(5)
            self.assertEqual(summary.read()['sports_balance'], 10)
        entered, release = threading.Event(), threading.Event()
        def fetch():
            entered.set()
            release.wait(5)
            return {'available': True, 'sports_balance': 20}
        client.fetch.side_effect = fetch
        with patch.dict('os.environ', {'LEYU_APP_LOGIN_NAME': 'second'}):
            try:
                value = summary.read()
                self.assertNotIn('sports_balance', value)
                self.assertTrue(entered.wait(2))
            finally:
                release.set()
                summary._thread.join(5)
            self.assertEqual(summary.read()['sports_balance'], 20)

    def test_unconfigured_and_failed_first_refresh_are_explicit(self):
        for factory, expected in ((lambda: None, '未配置'), (MagicMock(side_effect=OSError()), '刷新失败')):
            summary = AccountSummary(factory)
            summary.read()
            summary._thread.join(5)
            value = summary.read()
            self.assertFalse(value['available'])
            self.assertFalse(value['loading'])
            self.assertIn(expected, value['error'])
