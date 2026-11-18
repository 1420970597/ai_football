from datetime import datetime, timedelta, timezone
import unittest

from infra.profitability.reconcile_upstream import reconcile_window


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
