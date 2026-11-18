from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, patch

from infra.profitability.reconcile_upstream import collect, reconcile_window
from services.collector.leyu_protocol import LeyuError


START = datetime(2026, 11, 18, 2, tzinfo=timezone.utc)
END = START + timedelta(hours=12)


class ReconcileUpstreamTests(unittest.TestCase):
    def setUp(self):
        self.upstream = {
            'order_no': 'first', 'identifier_kind': 'order', 'state': 'settled',
            'placed_at': (START - timedelta(hours=1)).isoformat(), 'settled_at': START.isoformat(),
            'stake': '5', 'payout': '7.44', 'net_profit': '2.44', 'currency': None, 'fees': None,
        }
        self.local = dict(self.upstream, booked_at=START.isoformat(), stake='5.00')
        self.page = {'total': 1, 'has_more': False, 'malformed_records': 0, 'items': [self.upstream]}

    def test_matches_by_settlement_and_reports_missing_wallet_evidence(self):
        next_window = dict(self.local, order_no='next', booked_at=END.isoformat())
        output = reconcile_window(self.page, [self.local, next_window], START, END)
        self.assertTrue(output['complete_page'])
        self.assertEqual(output['matched'], 1)
        self.assertEqual(output['local_only'], 0)
        self.assertEqual(output['issues'], {'placed_before_window': 1, 'missing_currency': 1, 'missing_fees': 1})

    def test_incomplete_page_and_money_disagreement_are_visible(self):
        self.page.update(total=2, has_more=True)
        self.local['payout'] = '7.40'
        self.upstream['settled_at'] = None
        output = reconcile_window(self.page, [self.local], START, END)
        self.assertFalse(output['complete_page'])
        self.assertEqual(output['issues']['payout_difference'], 1)
        self.assertEqual(output['issues']['missing_upstream_settlement_time'], 1)

    def test_unmatched_orders_and_wrong_timestamp_are_visible(self):
        self.local['order_no'] = 'second'
        self.upstream['settled_at'] = END.isoformat()
        output = reconcile_window(self.page, [self.local], START, END)
        self.assertEqual((output['matched'], output['upstream_only'], output['local_only']), (0, 1, 1))
        self.assertEqual(output['issues']['outside_settlement_window'], 1)

    def test_duplicate_order_cannot_match_twice(self):
        self.page['items'].append(self.upstream)
        with self.assertRaises(ValueError):
            reconcile_window(self.page, [self.local], START, END)


class ReconcilePaginationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        first = {'order_no': 'first', 'identifier_kind': 'order', 'state': 'settled',
                 'placed_at': START.isoformat(), 'settled_at': START.isoformat(),
                 'stake': '5', 'payout': '7.44', 'net_profit': '2.44', 'currency': None, 'fees': None}
        second = dict(first, order_no='second')
        self.local = [dict(order, booked_at=START.isoformat()) for order in (first, second)]
        self.page1 = {'total': 2, 'has_more': True, 'malformed_records': 0, 'items': [first]}
        self.page2 = dict(self.page1, has_more=False, items=[second])
        self.client = AsyncMock()
        self.enterContext(patch('infra.profitability.reconcile_upstream.REQUEST_INTERVAL', 0))

    async def test_closed_single_window_can_reconcile_multiple_pages(self):
        self.client.orders.side_effect = [self.page1, self.page2]
        result, = await collect(self.client, START, self.local, window_count=1)
        self.assertTrue(result['complete_page'])
        self.assertEqual((2, 2), (result['pages_read'], result['matched']))
        self.assertEqual([1, 2], [call.kwargs['page'] for call in self.client.orders.await_args_list])
        self.assertTrue(all(call.kwargs['begin'] == START and call.kwargs['end'] == END
                            for call in self.client.orders.await_args_list))

    async def test_failed_or_unstable_pagination_preserves_partial_counts(self):
        for later, issue in (
            (LeyuError('upstream_rejected'), 'upstream_rejected'),
            (dict(self.page2, total=3), 'upstream_total_changed'),
            (dict(self.page2, items=self.page1['items']), 'duplicate_upstream_identity'),
        ):
            with self.subTest(issue=issue):
                self.client.orders.reset_mock()
                self.client.orders.side_effect = [self.page1, later]
                result, = await collect(self.client, START, self.local, window_count=1)
                self.assertFalse(result['complete_page'])
                self.assertEqual((1, 1, issue), (result['matched'], result['local_only'], result['scan_issue']))

    async def test_page_budget_reports_partial_scan(self):
        self.client.orders.side_effect = [self.page1, dict(self.page2, has_more=True)]
        with patch('infra.profitability.reconcile_upstream.PAGE_LIMIT', 2):
            result, = await collect(self.client, START, self.local, window_count=1)
        self.assertEqual(2, result['matched'])
        self.assertFalse(result['complete_page'])
        self.assertEqual('upstream_page_limit', result['scan_issue'])
