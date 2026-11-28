from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import AsyncMock, patch

from infra.profitability.reconcile_upstream import collect, reconcile_window
from infra.profitability.window_evidence import load_report, reconciliation, summarize, verify_archive
from services.collector.leyu_protocol import LeyuError


START = datetime(2026, 11, 18, 2, tzinfo=timezone.utc)
END = START + timedelta(hours=12)


class SettlementStreakEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'reconciliations').mkdir()
        self.snapshot = {
            'anchor': START.isoformat(), 'observed_at': (START + timedelta(hours=60)).isoformat(),
            'deadline_state': 'positive_receipts_all_four', 'rolling_four_positive_receipts': True,
            'windows': [],
        }
        for ordinal in range(5):
            begin = START + timedelta(hours=12 * ordinal)
            end = begin + timedelta(hours=12)
            digest = 'v1:' + str(ordinal) * 64
            self.snapshot['windows'].append({
                'ordinal': ordinal, 'closed': True, 'since': begin.isoformat(), 'until': end.isoformat(),
                'provider_net': '2.44', 'settled': 1, 'status': 'positive_receipts',
                'missing_fee_field': 0, 'missing_currency': 0, 'local_receipt_digest': digest,
            })
            self.write_scan(ordinal)

    def write_scan(self, ordinal, **changes):
        window = self.snapshot['windows'][ordinal]
        scan = {'begin': window['since'], 'end': window['until'], 'complete_page': True,
                'local_settled': 1, 'upstream_settled': 1, 'matched': 1, 'local_only': 0, 'upstream_only': 0,
                'settlement_window_attribution_complete': True, 'order_receipt_fields_reconciled': True,
                'local_receipt_digest': window['local_receipt_digest']}
        (self.root / 'reconciliations' / f'{ordinal}.json').write_text(json.dumps([scan | changes]))

    def test_initial_and_rolling_require_fresh_upstream_agreement_in_each_window(self):
        report = summarize(self.root, self.snapshot)
        self.assertTrue(report['initial_four_windows']['verified'])
        self.assertTrue(report['rolling_four_windows']['verified'])
        self.write_scan(0, local_receipt_digest='v1:' + 'f' * 64)
        report = summarize(self.root, self.snapshot)
        self.assertEqual([0], report['initial_four_windows']['unverified_windows'])
        self.assertFalse(report['initial_four_windows']['verified'])
        self.assertTrue(report['rolling_four_windows']['verified'])
        self.write_scan(4, settlement_window_attribution_complete=False)
        self.assertFalse(summarize(self.root, self.snapshot)['rolling_four_windows']['verified'])

    def test_rolling_windows_must_be_consecutive_and_locally_qualified(self):
        self.snapshot['rolling_four_positive_receipts'] = False
        report = summarize(self.root, self.snapshot)
        self.assertFalse(report['rolling_four_windows']['verified'])
        self.assertEqual([], report['rolling_four_windows']['unverified_windows'])
        self.snapshot['rolling_four_positive_receipts'] = True
        self.snapshot['windows'].pop(3)
        report = summarize(self.root, self.snapshot)
        self.assertEqual([1, 2, 3, 4], report['rolling_four_windows']['ordinals'])
        self.assertEqual([3], report['rolling_four_windows']['missing_closed_windows'])
        self.assertFalse(report['rolling_four_windows']['verified'])

    def test_positive_label_requires_positive_amount_and_fee_currency_evidence(self):
        window = self.snapshot['windows'][4]
        for replacements in ({'provider_net': '0'}, {'provider_net': '-2'}, {'provider_net': 'NaN'},
                             {'missing_fee_field': 1}, {'missing_currency': 1}):
            with self.subTest(replacements=replacements):
                self.snapshot['windows'][4] = window | replacements
                report = summarize(self.root, self.snapshot)
                self.assertEqual([4], report['rolling_four_windows']['unverified_windows'])
                self.assertFalse(report['rolling_four_windows']['verified'])

    def test_newer_complete_reconciliation_supersedes_an_older_verified_digest(self):
        window = self.snapshot['windows'][4]
        end = datetime.fromisoformat(window['until'])
        self.write_scan(4, observed_at=(end + timedelta(seconds=10)).isoformat())
        folder = self.root / 'reconciliations'
        original, = json.loads((folder / '4.json').read_text())
        old = folder / f"4.receipt-{window['local_receipt_digest'][3:]}.json"
        old.write_text(json.dumps([original]))
        corrected = original | {'observed_at': (end + timedelta(seconds=15)).isoformat(),
                                'order_receipt_fields_reconciled': False}
        (folder / '4.late-1.json').write_text(json.dumps([corrected]))
        self.assertFalse(summarize(self.root, self.snapshot)['rolling_four_windows']['verified'])
        incomplete = corrected | {'observed_at': (end + timedelta(seconds=20)).isoformat(), 'complete_page': False}
        (folder / '4.late-2.json').write_text(json.dumps([incomplete]))
        scan = summarize(self.root, self.snapshot)['windows'][4]['reconciliation']
        self.assertEqual('4.late-1.json', scan['file'])
        self.assertEqual(corrected['observed_at'], scan['observed_at'])

    def test_archive_replays_captured_files_and_detects_changes(self):
        state = self.root / 'state'
        state.mkdir()
        path = state / 'latest.json'
        path.write_text(json.dumps(self.snapshot))
        inputs = {}

        def read(path):
            return load_report(path, inputs, self.root)

        digest = self.snapshot['windows'][4]['local_receipt_digest']
        retry = self.root / 'reconciliations' / '4.late-1.json'
        primary = self.root / 'reconciliations' / '4.json'
        retry.write_bytes(primary.read_bytes())
        summary = summarize(self.root, read(path), read=read)
        archive = json.loads(json.dumps(summary | {'evidence_format': 1, 'input_files': inputs}, default=str))
        self.assertEqual('4.late-1.json', summary['windows'][4]['reconciliation']['file'])
        self.assertEqual(digest, json.loads(inputs['reconciliations/4.late-1.json']['json'])[0]['local_receipt_digest'])
        path.unlink()
        retry.unlink()
        self.assertEqual(summary, verify_archive(archive))
        archive['input_files']['state/latest.json']['json'] += '\n'
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            verify_archive(archive)
        archive['input_files']['state/latest.json']['json'] = inputs['state/latest.json']['json']
        archive['windows'][0]['settled'] += 1
        with self.assertRaisesRegex(ValueError, 'summary differs'):
            verify_archive(archive)


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

    def test_receipt_correction_retains_delta_codes_and_original_window(self):
        self.upstream.update(payout='5', net_profit='0', settled_at=(START + timedelta(minutes=20)).isoformat(),
                             provider_settlement_codes={'cancelType': 12, 'betStatus': 3}, pnl_basis='provider_reported_net')
        self.local.update(receipt_observed_at=(START + timedelta(minutes=2)).isoformat(),
                          settlement_codes='{"cancelType": 0, "betStatus": 1}', pnl_basis='provider_reported_net')
        output = reconcile_window(self.page, [self.local], START, END)
        self.assertEqual({'local_receipt_net': '2.44', 'upstream_receipt_net': '0', 'upstream_minus_local': '-2.44',
                          'currency': None}, output['receipt_amounts'])
        change, = output['settlement_discrepancies']
        self.assertEqual({'payout': '-2.44', 'net_profit': '-2.44'}, change['amount_delta'])
        self.assertEqual({'cancelType': 0, 'betStatus': 1}, change['before_codes'])
        self.assertEqual({'cancelType': 12, 'betStatus': 3}, change['after_codes'])
        self.assertTrue(change['same_native_placement'])
        self.assertTrue(change['upstream_full_principal_return'])
        self.assertTrue(change['upstream_settlement_after_local_observation'])
        self.assertTrue(change['both_settlements_in_window'])
        self.assertNotIn(self.local['order_no'], change)
        self.assertFalse(output['order_receipt_fields_reconciled'])
        self.assertIsNone(reconcile_window(self.page, [self.local | {'currency': 'USD'}], START, END)
                          ['receipt_amounts']['currency'])
        self.upstream['currency'] = 'CNY'
        self.assertIsNone(reconcile_window(self.page, [self.local | {'currency': 'USD'}], START, END)['receipt_amounts'])
        self.page['has_more'] = True
        self.assertIsNone(reconcile_window(self.page, [self.local], START, END)['receipt_amounts'])

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

    def test_digest_is_bound_to_current_local_fields_and_closed_window(self):
        local = dict(self.local, receipt_row_sha256='a' * 64)
        other = dict(local, order_no='next', booked_at=END.isoformat(), receipt_row_sha256='b' * 64)
        report = reconcile_window(self.page, [local, other], START, END)
        self.assertEqual('v1:' + sha256(b'a' * 64).hexdigest(), report['local_receipt_digest'])
        self.assertIsNone(reconcile_window(self.page, [self.local], START, END)['local_receipt_digest'])

    def test_count_agreement_does_not_verify_a_changed_ledger(self):
        previous = 'v1:' + 'a' * 64
        current = 'v1:' + 'b' * 64
        report = reconcile_window(self.page, [self.local], START, END)
        report['order_receipt_fields_reconciled'] = True
        window = {'ordinal': 0, 'settled': 1, 'local_receipt_digest': current}
        with TemporaryDirectory() as temporary:
            reports = Path(temporary)
            primary = reports / '0.json'
            primary.write_text(json.dumps([report]))
            legacy = reconciliation(reports, window, START, END)
            self.assertTrue(legacy['order_list_matches_current_count'])
            self.assertIsNone(legacy['local_receipt_digest_matches'])
            self.assertFalse(legacy['current_receipt_fields_reconciled'])
            primary.write_text(json.dumps([report | {'local_receipt_digest': previous}]))
            stale = reconciliation(reports, window, START, END)
            self.assertFalse(stale['local_receipt_digest_matches'])
            self.assertFalse(stale['current_receipt_fields_reconciled'])
            matching = reports / f'0.receipt-{current[3:]}.json'
            matching.write_text(json.dumps([report | {'local_receipt_digest': current}]))
            verified = reconciliation(reports, window, START, END)
            self.assertEqual(matching.name, verified['file'])
            self.assertTrue(verified['current_receipt_fields_reconciled'])

    def test_digest_retry_uses_the_best_available_reconciliation(self):
        digest = 'v1:' + 'a' * 64
        report = reconcile_window(self.page, [self.local], START, END) | {'local_receipt_digest': digest}
        window = {'ordinal': 0, 'settled': 1, 'local_receipt_digest': digest}
        with TemporaryDirectory() as temporary:
            reports = Path(temporary)
            initial = reports / f'0.receipt-{digest[3:]}.json'
            retry = reports / f'0.receipt-{digest[3:]}.retry.json'
            initial.write_text(json.dumps([report]))
            retry.write_text(json.dumps([report | {'complete_page': False}]))
            self.assertEqual(initial.name, reconciliation(reports, window, START, END)['file'])
            retry.write_text(json.dumps([report | {'order_receipt_fields_reconciled': True}]))
            result = reconciliation(reports, window, START, END)
            self.assertEqual(retry.name, result['file'])
            self.assertTrue(result['current_receipt_fields_reconciled'])

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
