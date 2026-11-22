"""Contract and arithmetic tests for C01, P01, C02 and P05 experiments."""

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from infra.profitability.experiments import attribution, clock, pricing, threestage

START = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
ONE_MINUTE = timedelta(minutes=1)


def iso(moment):
    return moment.isoformat()


def frame(slot, rows, observed=None, begin=None, stop=None):
    return {
        'slot': iso(slot),
        'begin': iso(begin or slot - timedelta(seconds=90)),
        'end': iso(stop or slot - timedelta(seconds=30)),
        'observed_at': iso(observed or slot + timedelta(seconds=9)),
        'capture_mode': 'paper_preflight',
        'rows': rows,
    }


def paper_row(slot, signal_id='signal-1', quote_time=None, checked=None, **extra):
    row = {
        'signal_id': signal_id,
        'created_at': iso(slot - timedelta(seconds=40)),
        'quote_time': iso(quote_time or slot - timedelta(seconds=60)),
        'native_quote_checked_at': iso(checked or slot + timedelta(seconds=11)),
        'recommendation_price': '2.10',
        'native_selected_price': '2.20',
        'native_prices': {'Over': '2.20', 'Under': '1.70'},
        'status': 'native_preflight_available',
        'sport': 'football',
        'phase': 'live',
        'market_kind': 'total',
        'period': 'first_half',
        'line': '0.75',
        'selection': 'over',
    }
    row.update(extra)
    return row


def native_document(rows, begin=None, end=None, cutoff=None):
    return {
        'begin': iso(begin or START - timedelta(days=1)),
        'end': iso(end or START),
        'cutoff': iso(cutoff or START + timedelta(hours=1)),
        'rows': rows,
    }


def native_row(order_id='order-1', signal=None, preflight=None, placed=None, **extra):
    signal = signal or START - timedelta(seconds=30)
    preflight = preflight or START - timedelta(seconds=10)
    placed = placed or START - timedelta(seconds=5)
    row = {
        'order_id': order_id,
        'signal_at': iso(signal),
        'preflight_at': iso(preflight),
        'placed_at': iso(placed),
        'signal_price': '2.10',
        'preflight_price': '2.15',
        'sport': 'football',
        'market_kind': 'total',
        'receipts': [],
    }
    row.update(extra)
    return row


class ClockTests(unittest.TestCase):
    def test_source_age_and_paper_preflight_window(self):
        result = clock.minute_archive(
            [frame(START, [paper_row(START)])],
            start=START, end=START + ONE_MINUTE,
        )
        self.assertEqual(result['problems'], {})
        self.assertEqual(result['denominator']['usable_rows'], 1)
        self.assertEqual(result['source_age']['observations'], 1)
        self.assertEqual(result['source_age']['p50'], Decimal('20.0'))
        self.assertEqual(result['quote_to_observation']['p50'], Decimal('69.0'))
        # The paper preflight happens after the frame observation, never before it.
        self.assertEqual(result['observed_to_preflight']['observations'], 1)
        self.assertEqual(result['observed_to_preflight']['p50'], Decimal('2.0'))

    def test_preflight_after_fifteen_seconds_is_not_a_preflight_measurement(self):
        late = paper_row(START, checked=START + timedelta(seconds=25))
        result = clock.minute_archive([frame(START, [late])], start=START, end=START + ONE_MINUTE)
        self.assertEqual(result['problems']['preflight_check_too_late'], 1)
        self.assertEqual(result['observed_to_preflight']['observations'], 0)
        # The visible quote is still usable for source age.
        self.assertEqual(result['denominator']['usable_rows'], 1)

    def test_preflight_before_observation_is_rejected(self):
        early = paper_row(START, checked=START + timedelta(seconds=3))
        result = clock.minute_archive([frame(START, [early])], start=START, end=START + ONE_MINUTE)
        self.assertEqual(result['problems']['preflight_before_observation'], 1)
        self.assertEqual(result['observed_to_preflight']['observations'], 0)

    def test_quote_after_created_and_created_outside_slot_are_excluded(self):
        after = paper_row(START, signal_id='late-quote',
                          quote_time=START - timedelta(seconds=30))
        outside = paper_row(START, signal_id='outside',
                            created_at=iso(START - timedelta(seconds=5)))
        result = clock.minute_archive(
            [frame(START, [after, outside])], start=START, end=START + ONE_MINUTE)
        self.assertEqual(result['problems']['quote_after_created'], 1)
        self.assertEqual(result['problems']['created_outside_slot'], 1)
        self.assertEqual(result['denominator']['usable_rows'], 0)

    def test_missing_signal_identifier_is_filtered(self):
        row = paper_row(START)
        row.pop('signal_id')
        result = clock.minute_archive([frame(START, [row])], start=START, end=START + ONE_MINUTE)
        self.assertEqual(result['problems']['missing_signal_identifier'], 1)
        self.assertEqual(result['denominator']['archived_rows'], 0)

    def test_expected_slots_come_from_fixed_interval_not_observed_bounds(self):
        frames = [frame(START, []), frame(START + 2 * ONE_MINUTE, [])]
        result = clock.minute_archive(frames, start=START, end=START + 3 * ONE_MINUTE)
        self.assertEqual(result['denominator']['expected_slots'], 3)
        self.assertEqual(result['denominator']['distinct_slots'], 2)
        self.assertEqual(result['denominator']['missing_slots'], 1)
        self.assertEqual(result['denominator']['capture_share'], Decimal(2) / Decimal(3))

    def test_invalid_frame_boundary_and_observation_are_excluded(self):
        bad_boundary = frame(START, [], stop=START - timedelta(seconds=10))
        bad_observation = frame(START + ONE_MINUTE, [], observed=START + ONE_MINUTE + timedelta(seconds=40))
        result = clock.minute_archive([bad_boundary, bad_observation], start=START, end=START + 2 * ONE_MINUTE)
        self.assertEqual(result['denominator']['invalid_frames'], 2)
        self.assertEqual(result['denominator']['distinct_slots'], 0)
        self.assertEqual(result['denominator']['missing_slots'], 2)

    def test_frozen_order_clock_and_exclusions(self):
        rows = [
            native_row('ok'),
            native_row('reverse', signal=START, preflight=START - timedelta(seconds=5)),
            native_row('late-entry', placed=START + timedelta(hours=2)),
        ]
        result = clock.frozen_orders(rows, START + timedelta(hours=1))
        self.assertEqual(result['denominator']['complete_clock'], 1)
        self.assertEqual(result['problems']['signal_after_preflight'], 1)
        self.assertEqual(result['problems']['entry_at_or_after_cutoff'], 1)
        self.assertEqual(result['to_preflight']['p50'], Decimal('20.0'))
        self.assertEqual(result['to_entry']['p50'], Decimal('5.0'))

    def test_analyze_keeps_queues_independent(self):
        report = clock.analyze(
            native_document([native_row()]),
            [frame(START, [paper_row(START)])],
            start=START, end=START + ONE_MINUTE,
        )
        self.assertEqual(report['experiment'], 'C01')
        self.assertIsNone(report['cross_queue']['common_identifier'])
        self.assertEqual(report['coverage']['included']['orders'], 1)
        self.assertEqual(report['coverage']['included']['archive_rows'], 1)


class AttributionTests(unittest.TestCase):
    def test_rejection_reasons_keep_group_denominators(self):
        rows = [
            paper_row(START, signal_id='a', status='source_ineligible'),
            paper_row(START, signal_id='b', status='source_ineligible', phase='scheduled'),
            paper_row(START, signal_id='c', status='unsupported_contract'),
            paper_row(START, signal_id='a', status='duplicate'),
            paper_row(START, signal_id='d', status=None),
        ]
        row = rows[-1]
        row.pop('signal_id')
        report = attribution.analyze(
            [frame(START, rows)], start=START, end=START + ONE_MINUTE)
        self.assertEqual(report['denominator']['unique_opportunities'], 3)
        self.assertEqual(report['denominator']['duplicate_rows'], 1)
        self.assertEqual(report['problems']['missing_signal_identifier'], 1)
        self.assertEqual(report['reasons']['source_ineligible'], 2)
        self.assertEqual(report['reasons']['unsupported_contract'], 1)
        self.assertEqual(report['cross_tab']['total'], 3)
        live = [cell for cell in report['cross_tab']['cells']
                if cell['dimensions']['phase'] == 'live']
        self.assertEqual({cell['group_denominator'] for cell in live}, {2})
        self.assertIn({'dimensions': {'phase': 'live', 'market_kind': 'total', 'period': 'first_half',
                                      'line': '0.75', 'selection': 'over', 'league': 'unknown'},
                       'count': 2},
                      report['cross_tab']['group_denominators'])

    def test_missing_league_is_unknown_not_dropped(self):
        report = attribution.analyze(
            [frame(START, [paper_row(START)])], start=START, end=START + ONE_MINUTE)
        self.assertEqual(report['denominator']['league_unknown'], 1)
        self.assertEqual(report['cross_tab']['cells'][0]['dimensions']['league'], 'unknown')

    def test_windows_have_their_own_denominators(self):
        rows = [paper_row(START, signal_id='a', status='source_ineligible'),
                paper_row(START + timedelta(days=1), signal_id='b', status='unsupported_contract')]
        report = attribution.analyze(
            [frame(START, [rows[0]]), frame(START + timedelta(days=1), [rows[1]])],
            start=START, end=START + timedelta(days=2))
        self.assertEqual([window['denominator'] for window in report['windows']], [1, 1])

    def test_counterfactual_is_not_estimable_without_history(self):
        report = attribution.analyze(
            [frame(START, [paper_row(START, status='source_ineligible')])],
            start=START, end=START + ONE_MINUTE)
        self.assertEqual(report['counterfactual']['status'], 'not_estimable')
        self.assertNotIn('recovered_share_by_horizon', report['counterfactual'])

    def test_counterfactual_uses_same_opportunity_history(self):
        row = paper_row(START, status='source_ineligible')
        row['native_state_history'] = [
            {'at': iso(START - timedelta(seconds=30)), 'status': 'native_preflight_available', 'price': '2.2'},
            {'at': iso(START + timedelta(seconds=10)), 'status': 'native_preflight_available', 'price': '2.3'},
        ]
        report = attribution.analyze([frame(START, [row])], start=START, end=START + ONE_MINUTE)
        self.assertEqual(report['counterfactual']['status'], 'measured')
        self.assertEqual(report['counterfactual']['recovered_by_horizon'],
                         {'30': 1, '45': 1, '60': 1})


def decision(status, cost=None, sources=(), reasons=()):
    result = {'status': status, 'reasons': list(reasons), 'complete_sources': list(sources)}
    if cost is not None:
        result['account_cost_approval'] = {'cost_per_unit': str(cost), 'schedule': {'currency': 'CNY'}}
    return result


class PricingTests(unittest.TestCase):
    def with_verified_cost(self, status, sources):
        row = paper_row(START, paper_account_binding='a' * 64, paper_amount_passing_native_limits='5')
        proof = b'inert fee schedule test fixture'
        digest = sha256(proof).hexdigest()
        sample = decision(status, cost='0.01', sources=sources)
        sample['account_cost_approval']['schedule'].update(
            evidence_sha256=digest, account_binding='a' * 64, stake_min='1', stake_max='6',
            effective_from='2026-11-19T00:00:00Z',
        )
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / digest).write_bytes(proof)
            return pricing.analyze([frame(START, [row])], decisions={'signal-1': sample}, cost_root=root)

    def test_verified_cost_and_dual_sources_are_kept_apart_from_native_price(self):
        report = self.with_verified_cost('price_eligible', ['a', 'b'])
        self.assertEqual(report['classes']['price_eligible'], 1)
        self.assertEqual(report['cost_evidence'], {'certified': 1})
        self.assertEqual(report['source_evidence'], {'dual_complete': 1})
        self.assertEqual(report['native_price_comparison']['status'], 'measured')
        self.assertEqual(report['native_price_comparison']['delta']['median_delta'], Decimal('0.10'))

    def test_below_minimum_is_not_the_same_as_missing_evidence(self):
        report = pricing.analyze(
            [frame(START, [paper_row(START)])],
            decisions={'signal-1': decision('below_minimum_odds', reasons=['low'])},
        )
        self.assertEqual(report['classes']['missing_cost_and_sources'], 1)
        self.assertEqual(report['cost_evidence'], {'missing': 1})
        self.assertEqual(report['source_evidence'], {'missing': 1})
        self.assertEqual(self.with_verified_cost('below_minimum_odds', ['a', 'b'])['classes'],
                         {'below_minimum_odds': 1})

    def test_unknown_cost_is_never_counted_as_free(self):
        report = pricing.analyze(
            [frame(START, [paper_row(START)])],
            decisions={'signal-1': decision('price_eligible', sources=['a', 'b'])},
        )
        self.assertEqual(report['classes']['missing_cost'], 1)
        self.assertNotIn('price_eligible', report['classes'])

    def test_missing_dual_sources_has_its_own_class(self):
        report = self.with_verified_cost('price_eligible', [])
        self.assertEqual(report['classes']['missing_dual_sources'], 1)

    def test_out_of_window_preflight_is_unusable(self):
        row = paper_row(START, checked=START + timedelta(seconds=40))
        report = pricing.analyze([frame(START, [row])])
        self.assertEqual(report['classes']['unusable_quote'], 1)
        self.assertEqual(report['unusable']['preflight_check_outside_window'], 1)

    def test_non_preflight_rows_without_decision_are_ignored(self):
        row = paper_row(START, status='source_ineligible')
        report = pricing.analyze([frame(START, [row])])
        self.assertEqual(report['denominator']['native_preflight_rows'], 0)


def receipt(**extra):
    value = {
        'stake': '10',
        'net': '9.50',
        'fees': '0.50',
        'payout': '20.00',
        'currency': 'CNY',
        'outcome_share': '1',
        'observed_at': iso(START + timedelta(minutes=15)),
    }
    value.update(extra)
    return value


class ThreeStageTests(unittest.TestCase):
    def test_missing_fill_price_reports_only_the_two_visible_stages(self):
        doc = native_document([native_row(receipts=[receipt()])])
        report = threestage.analyze(doc)
        self.assertEqual(report['classes']['missing_fill'], 1)
        self.assertEqual(report['classes']['partial_bridge_only'], 1)
        self.assertEqual(report['denominator']['fill_recomputed'], 0)
        self.assertIn('recommendation_to_preflight', report['segments'])
        self.assertNotIn('preflight_to_fill', report['segments'])
        self.assertEqual(report['segments']['recommendation_to_preflight']['p50'], Decimal('0.05'))
        self.assertEqual(report['residual']['status'], 'insufficient_evidence')

    def test_visible_paper_stage_is_reported_without_fill(self):
        report = threestage.analyze(
            native_document([native_row()]),
            [frame(START, [paper_row(START)])],
        )
        self.assertEqual(report['paper_visible']['rows'], 1)
        self.assertEqual(report['paper_visible']['fill'], 'not archived')
        self.assertEqual(report['paper_visible']['delta']['p50'], Decimal('0.10'))

    def test_full_win_input_is_recomputed_and_residual_is_visible(self):
        row = native_row(receipts=[receipt()], fill_price='2.00')
        report = threestage.analyze(native_document([row]))
        self.assertEqual(report['classes']['recomputed'], 1)
        self.assertEqual(report['denominator']['fill_recomputed'], 1)
        self.assertEqual(report['residual']['value']['p50'], Decimal('0.00'))
        self.assertEqual(report['segments']['preflight_to_fill']['p50'], Decimal('-0.15'))

    def test_half_win_and_loss_shares_recompute_from_verified_share(self):
        half = native_row('half', receipts=[receipt(outcome_share='0.5', stake='8', fees='0',
                                                    net='4', payout='12')], fill_price='2.00')
        loss = native_row('loss', receipts=[receipt(outcome_share='-1', stake='10', fees='0',
                                                    net='-10', payout='0')], fill_price='2.00')
        report = threestage.analyze(native_document([half, loss]))
        self.assertEqual(report['classes']['recomputed'], 2)
        self.assertEqual(report['residual']['value']['p50'], Decimal('0'))

    def test_unknown_fees_are_not_measured_as_profit(self):
        row = native_row(receipts=[receipt(fees=None)], fill_price='2.00')
        report = threestage.analyze(native_document([row]))
        self.assertEqual(report['classes']['missing_fees'], 1)
        self.assertEqual(report['denominator']['fill_recomputed'], 0)
        self.assertEqual(report['residual']['status'], 'insufficient_evidence')

    def test_zero_refund_and_missing_currency_are_reported(self):
        refund = native_row('refund', receipts=[receipt(outcome_share='0', net='0', fees='0',
                                                         payout='0')], fill_price='2.00')
        no_currency = native_row('currency', receipts=[receipt(currency=None)], fill_price='2.00')
        report = threestage.analyze(native_document([refund, no_currency]))
        self.assertEqual(report['classes']['zero_refund'], 1)
        self.assertEqual(report['classes']['missing_currency'], 1)

    def test_mismatched_contract_is_not_recomputed(self):
        row = native_row(receipts=[receipt()], fill_price='2.00')
        row['receipts'][0]['contract'] = {'market_kind': 'handicap'}
        report = threestage.analyze(native_document([row]))
        self.assertEqual(report['classes']['mismatched_contract'], 1)
        self.assertEqual(report['denominator']['fill_recomputed'], 0)

    def test_late_revisions_do_not_replace_a_visible_fill(self):
        early = receipt()
        late = receipt(net='200', observed_at=iso(START + timedelta(hours=2)))
        row = native_row(receipts=[early, late], fill_price='2.00')
        report = threestage.analyze(native_document([row]))
        self.assertEqual(report['classes']['receipt_outside_cutoff'], 1)
        self.assertEqual(report['denominator']['fill_recomputed'], 1)
        self.assertEqual(report['residual']['value']['p50'], Decimal(0))

    def test_changing_net_does_not_change_or_derive_the_fill_price(self):
        row = native_row(receipts=[receipt(net='99')], fill_price='2.00')
        report = threestage.analyze(native_document([row]))
        self.assertEqual(report['denominator']['fill_recomputed'], 1)
        self.assertEqual(report['residual']['value']['p50'], Decimal('89.50'))


if __name__ == '__main__':
    unittest.main()
