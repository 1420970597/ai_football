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
        self.assertTrue(output['settlement_window_attribution_complete'])
        self.assertFalse(output['order_receipt_fields_reconciled'])
        self.assertEqual(output['matched'], 1)
        self.assertEqual(output['local_only'], 0)
        self.assertEqual(output['issues'], {'placed_before_window': 1, 'missing_currency': 1, 'missing_fees': 1})

    def test_matched_placement_and_first_settled_receipt_bound_missing_native_time(self):
        placed = START + timedelta(hours=2)
        observed = placed + timedelta(seconds=19)
        self.upstream.update(placed_at=placed.isoformat(), settled_at=None, currency='CNY', fees='0')
        local = dict(self.upstream, booked_at=observed.isoformat(), first_settled_observed_at=observed.isoformat())
        output = reconcile_window(self.page, [local], START, END)
        self.assertTrue(output['settlement_window_attribution_complete'])
        self.assertEqual(output['bounded_without_upstream_time'], 1)
        self.assertEqual(output['issues']['missing_upstream_settlement_time'], 1)
        self.assertFalse(output['order_receipt_fields_reconciled'])

        for change in (
            {'placed_at': None},
            {'placed_at': (placed - timedelta(seconds=1)).isoformat()},
            {'first_settled_observed_at': None},
            {'first_settled_observed_at': (placed - timedelta(microseconds=1)).isoformat()},
            {'first_settled_observed_at': END.isoformat()},
        ):
            with self.subTest(change=change):
                output = reconcile_window(self.page, [local | change], START, END)
                self.assertFalse(output['settlement_window_attribution_complete'])
                self.assertEqual(output['bounded_without_upstream_time'], 0)

        self.upstream['placed_at'] = (START - timedelta(microseconds=1)).isoformat()
        local['placed_at'] = self.upstream['placed_at']
        output = reconcile_window(self.page, [local], START, END)
        self.assertFalse(output['settlement_window_attribution_complete'])
        self.assertEqual(output['bounded_without_upstream_time'], 0)

    def test_matching_currency_and_decimal_fee_reconcile_order_fields(self):
        self.upstream.update(currency='CNY', fees='0.10')
        local_zone = timezone(timedelta(hours=8))
        self.local.update(currency='CNY', fees='0.100',
                          placed_at=(START - timedelta(hours=1)).astimezone(local_zone).isoformat())
        output = reconcile_window(self.page, [self.local], START, END)
        self.assertTrue(output['order_receipt_fields_reconciled'])
        self.assertEqual({'placed_before_window': 1}, output['issues'])

    def test_placement_timestamp_gap_or_disagreement_prevents_receipt_reconciliation(self):
        self.upstream.update(currency='CNY', fees='0')
        self.local.update(currency='CNY', fees='0')
        for local_placed, issue in ((None, 'recoverable_local_placement_time'),
                                    (START.isoformat(), 'placement_time_difference')):
            with self.subTest(issue=issue):
                output = reconcile_window(self.page, [self.local | {'placed_at': local_placed}], START, END)
                self.assertTrue(output['settlement_window_attribution_complete'])
                self.assertFalse(output['order_receipt_fields_reconciled'])
                self.assertEqual(1, output['issues'][issue])

        self.upstream['placed_at'] = None
        output = reconcile_window(self.page, [self.local], START, END)
        self.assertTrue(output['settlement_window_attribution_complete'])
        self.assertFalse(output['order_receipt_fields_reconciled'])
        self.assertEqual({'missing_upstream_placement_time': 1}, output['issues'])

    def test_upstream_settlement_before_placement_blocks_window_attribution(self):
        self.upstream.update(currency='CNY', fees='0', placed_at=(START + timedelta(seconds=1)).isoformat())
        local = dict(self.upstream, booked_at=START.isoformat())
        output = reconcile_window(self.page, [local], START, END)
        self.assertTrue(output['complete_page'])
        self.assertFalse(output['settlement_window_attribution_complete'])
        self.assertFalse(output['order_receipt_fields_reconciled'])
        self.assertEqual({'upstream_settlement_before_placement': 1}, output['issues'])

    def test_cost_differences_and_missing_local_fields_block_reconciliation(self):
        self.upstream.update(currency='CNY', fees='0.10')
        for local, issues in [
            (dict(currency='USD', fees='0'), {'currency_difference': 1, 'fees_difference': 1}),
            (dict(currency=None, fees=None), {'recoverable_local_currency': 1, 'recoverable_local_fees': 1}),
        ]:
            with self.subTest(local=local):
                output = reconcile_window(self.page, [self.local | local], START, END)
                self.assertTrue(output['complete_page'])
                self.assertFalse(output['order_receipt_fields_reconciled'])
                self.assertEqual({'placed_before_window': 1, **issues}, output['issues'])

    def test_multiple_currencies_cannot_reconcile_a_single_currency_window(self):
        self.upstream.update(currency='CNY', fees='0')
        self.local.update(currency='CNY', fees='0')
        another = dict(self.upstream, order_no='second', currency='USD')
        self.page.update(total=2, items=[self.upstream, another])
        output = reconcile_window(self.page, [self.local, dict(another, booked_at=START.isoformat())], START, END)
        self.assertTrue(output['complete_page'])
        self.assertFalse(output['order_receipt_fields_reconciled'])
        self.assertEqual(2, output['issues']['mixed_upstream_currencies'])

    def test_incomplete_page_and_money_disagreement_are_visible(self):
        self.page.update(total=2, has_more=True)
        self.local['payout'] = '7.40'
        self.upstream['settled_at'] = None
        output = reconcile_window(self.page, [self.local], START, END)
        self.assertFalse(output['complete_page'])
        self.assertFalse(output['settlement_window_attribution_complete'])
        self.assertEqual(output['issues']['payout_difference'], 1)
        self.assertEqual(output['issues']['missing_upstream_settlement_time'], 1)

    def test_unmatched_orders_and_wrong_timestamp_are_visible(self):
        self.local['order_no'] = 'second'
        self.upstream['settled_at'] = END.isoformat()
        output = reconcile_window(self.page, [self.local], START, END)
        self.assertEqual((output['matched'], output['upstream_only'], output['local_only']), (0, 1, 1))
        self.assertEqual(output['issues']['outside_settlement_window'], 1)
        self.assertFalse(output['settlement_window_attribution_complete'])

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
