"""Cash reservations tested without a venue, credentials or debit requests."""
import tempfile
import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from store.order_store import OrderStore, money_cents, validated_budget
from store.sql_backend import SQLiteAdapter, StorageBackendError


class ExposureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'orders.sqlite3'
        self.store = self.open_store()

    def open_store(self):
        store = OrderStore(SQLiteAdapter.open(self.path), path=self.path)
        store.initialize()
        self.addCleanup(store.close)
        return store

    def budget(self, **patch):
        return {'source': 'leyu_ybty', 'currency': 'CNY', 'complete': True,
                'observed_at': time.time(), 'available': 100, 'unsettled': [], **patch}

    def claim(self, identity, *, match='m', stake=2, store=None, budget=None):
        return (store or self.store).claim(identity, {'match_id': match, 'stake': stake, 'status': 'sending'},
            identity, 'account', budget=self.budget() if budget is None else budget,
            total_fraction=.1, match_fraction=.04)

    def held(self):
        return self.store.adapter.execute('SELECT sum(cents) FROM order_reservations WHERE held=1').fetchone()[0] or 0

    def test_money_precision_and_invalid_values(self):
        self.assertEqual(money_cents('2.999'), 299)
        for value in (None, True, -1, 'NaN', 'Infinity'):
            with self.subTest(value=value), self.assertRaises(StorageBackendError):
                money_cents(value)

    def test_duplicate_and_rejected_claims_preserve_correct_budget(self):
        self.assertTrue(self.claim('one')[0])
        self.assertFalse(self.claim('one')[0])
        self.assertEqual(self.held(), 200)
        self.store.update('one', 'rejected', {'status': 'rejected'})
        self.assertEqual(self.held(), 0)
        self.assertFalse(self.claim('one')[0])
        self.assertTrue(self.claim('two', stake=4)[0])

    def test_unknown_survives_restart_and_cannot_reuse_money(self):
        self.claim('one', stake=4)
        self.store.update('one', 'unknown', {'status': 'unknown', 'stake': 4, 'match_id': 'm'})
        reopened = self.open_store()
        with self.assertRaisesRegex(StorageBackendError, '敞口'):
            self.claim('two', store=reopened)
        self.assertEqual(self.held(), 400)

    def test_concurrent_connections_cannot_overrun_match_or_global_budget(self):
        stores = [self.open_store() for _ in range(8)]
        def run(pair):
            i, store = pair
            try:
                return self.claim('id'+str(i), store=store, match='m'+str(i % 3))[0]
            except StorageBackendError:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(run, enumerate(stores)))
        self.assertEqual(sum(results), 5)
        self.assertEqual(self.held(), 1000)
        self.assertEqual(self.store.adapter.execute('SELECT max(cents) FROM '
            '(SELECT sum(cents) cents FROM order_reservations GROUP BY match_id)').fetchone()[0], 400)

    def test_external_orders_are_budgeted_and_known_receipts_not_double_counted(self):
        self.claim('one')
        self.store.update('one', 'accepted', {'status': 'accepted', 'order_no': 'venue-one'})
        snapshot = self.budget(available=98, unsettled=[{'order_no': 'venue-one', 'match_id': 'm', 'amount': 2}])
        self.assertTrue(self.claim('two', budget=snapshot)[0])
        with self.assertRaises(StorageBackendError):
            self.claim('three', budget=snapshot)

    def test_partial_stale_or_mismatched_snapshot_blocks(self):
        for snapshot in (self.budget(complete=False), self.budget(observed_at=time.time()-31),
                         self.budget(available=None), self.budget(unsettled=[{'amount': 2}])):
            with self.assertRaises(StorageBackendError):
                self.claim('one', budget=snapshot)
        self.assertEqual(self.held(), 0)

    def test_only_authoritative_exact_settled_receipt_releases(self):
        self.claim('one', stake=4)
        self.store.update('one', 'accepted', {'status': 'accepted', 'order_no': 'venue-one'})
        self.assertEqual(self.store.reconcile_settled('account', [{'source':'leyu_app','status':'settled','order_no':'venue-one'}]), 0)
        self.assertEqual(self.store.reconcile_settled('other', [{'source':'leyu_ybty','status':'settled','order_no':'venue-one'}]), 0)
        receipt = {'source':'leyu_ybty','status':'settled','order_no':'venue-one'}
        self.assertEqual(self.store.reconcile_settled('account', [receipt]), 1)
        self.assertEqual(self.store.reconcile_settled('account', [receipt]), 0)
        self.assertEqual(self.held(), 0)

    def test_authorization_failure_rolls_back_reservation_and_order(self):
        def fail(_callback):
            raise ValueError('settings changed')
        with self.assertRaises(ValueError):
            self.store.claim('one', {'match_id':'m','stake':2}, 'one', 'account',
                             budget=self.budget(), authorize=fail)
        self.assertEqual(self.held(), 0)
        self.assertIsNone(self.store.existing_order('one','account'))

    def test_restart_backfill_releases_only_explicit_settled_receipt(self):
        for identity, status, no in [('old', 'accepted', 'old-native'),
                                     ('unknown', 'unknown', None)]:
            self.store.adapter.execute(
                'INSERT INTO orders(identity,at,status,payload,selection_key,account_key) '
                'VALUES (?,?,?,?,?,?)',
                (identity, time.time(), status, json.dumps({'stake': 2, 'match_id': 'm',
                 'order_no': no}), identity, 'account'))
        restarted = self.open_store()
        self.assertEqual(restarted.reconcile_settled('account', []), 0)
        self.assertEqual(restarted.reservation_summary('account'), {
            'held_count': 2, 'held_cents': 400, 'held_without_order_number': 1})
        self.assertEqual(restarted.reconcile_settled('account', [{
            'order_no': 'old-native', 'source': 'leyu_ybty', 'status': 'settled'}]), 1)
        again = self.open_store()
        self.assertEqual(again.reconcile_settled('account', []), 0)
        self.assertEqual(again.reservation_summary('account'), {
            'held_count': 1, 'held_cents': 200, 'held_without_order_number': 1})

    def test_wallet_fallback_is_never_accepted_as_native_snapshot(self):
        with self.assertRaises(StorageBackendError):
            validated_budget(self.budget(source='leyu_app'))


if __name__ == '__main__':
    unittest.main()
