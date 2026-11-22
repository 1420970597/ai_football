import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from infra.profitability.experiments.risk import capacity, exposure

START = datetime(2026, 11, 1, tzinfo=timezone.utc)


def stamp(value):
    return value.isoformat()


def receipt(stake, payout, net, observed, *, settled=None, fees=None, currency=None):
    return {
        'stake': str(stake), 'payout': str(payout), 'net': str(net),
        'fees': None if fees is None else str(fees),
        'currency': currency,
        'settled_at': None if settled is None else stamp(settled),
        'observed_at': stamp(observed),
    }


def order(name, placed, *, fixture=None, receipts=(), **extra):
    row = {
        'order_id': name, 'fixture_id': fixture or name, 'expert_id': 'expert',
        'sport': 'football', 'market_kind': 'handicap',
        'placed_at': stamp(placed),
        'receipts': list(receipts),
    }
    row.update(extra)
    return row


def document(rows, end=START + timedelta(days=2)):
    return {'begin': stamp(START), 'end': stamp(end), 'cutoff': stamp(end), 'rows': rows}


def settled(placed, *, stake='10', payout='15', fees=None, observed=None, settled_at=None):
    observed = observed or placed + timedelta(hours=1)
    settled_at = settled_at or placed + timedelta(minutes=30)
    net = Decimal(payout) - Decimal(stake)
    if fees is not None:
        net -= Decimal(fees)
    return receipt(stake, payout, net, observed, settled=settled_at, fees=fees)


class CapacityTest(unittest.TestCase):
    def test_only_valid_receipts_contribute_and_unknown_fees_stay_unknown(self):
        placed = START + timedelta(hours=1)
        rows = [
            order('ok', placed, receipts=[settled(placed)]),
            order('invalid', placed, receipts=[settled(placed, observed=placed - timedelta(minutes=1))]),
            order('none', placed),
        ]
        result = capacity(document(rows))
        self.assertEqual({'rows': 3, 'valid_receipt_orders': 1,
                          'orders_without_valid_receipt': 2}, result['denominator'])
        self.assertEqual(Decimal('10'), result['amounts']['recognized_turnover'])
        self.assertIsNone(result['amounts']['verified_fee_amount'])
        self.assertEqual(1, result['amounts']['orders_with_unknown_fees'])
        self.assertEqual('not_estimable_from_accepted_cohort', result['unaccepted_opportunities'])

    def test_stake_fee_and_profit_slippage_matrices_are_separate(self):
        placed = START + timedelta(hours=1)
        result = capacity(document([order('ok', placed, receipts=[settled(placed)])]))
        limits = {row['stake_limit']: row for row in result['stake_limit_matrix']}
        self.assertEqual(Decimal('2.5'), limits[Decimal('5')]['gross_units_at_limit'])
        self.assertEqual(Decimal('5'), limits[Decimal('5')]['effective_turnover'])
        fees = {row['fee_rate_on_stake']: row for row in result['fee_stress_matrix']}
        self.assertEqual(Decimal('4.9'), fees[Decimal('0.01')]['units_after_assumed_fee'])
        slip = {row['profit_part_slippage']: row for row in result['profit_slippage_matrix']}
        self.assertEqual(Decimal('4.75'), slip[Decimal('0.05')]['units_after_slippage'])
        self.assertIn('scenario stresses', ' '.join(result['assumptions']))

    def test_known_fee_is_counted_once_and_zero_payout_refund_is_valid(self):
        placed = START + timedelta(hours=1)
        rows = [
            order('fee', placed, receipts=[settled(placed, payout='15', fees='0.5')]),
            order('refund', placed + timedelta(minutes=5), receipts=[
                receipt('10', '10', '0', placed + timedelta(hours=1),
                        settled=placed + timedelta(minutes=30))]),
        ]
        result = capacity(document(rows))
        self.assertEqual(1, result['amounts']['orders_with_known_fees'])
        self.assertEqual(Decimal('0.5'), result['amounts']['verified_fee_amount'])
        self.assertEqual(Decimal('20'), result['amounts']['recognized_turnover'])
        self.assertEqual(Decimal('0.5'), result['amounts']['recognized_gross_units'])


class ExposureTest(unittest.TestCase):
    def test_release_is_strictly_before_the_next_entry(self):
        place = START + timedelta(hours=1)
        release = place + timedelta(hours=1)
        rows = [
            order('a', place, fixture='same', receipts=[settled(place, observed=release)]),
            order('b', release, fixture='same', receipts=[settled(release, observed=release + timedelta(hours=1))]),
            order('c', release + timedelta(seconds=1), fixture='same',
                  receipts=[settled(release, observed=release + timedelta(hours=2))]),
        ]
        result = exposure(document(rows))
        self.assertEqual(3, result['policies']['all']['entries'])
        self.assertEqual(1, result['policies']['one_per_fixture']['blocked'])
        self.assertEqual(2, result['policies']['one_per_fixture']['entries'])
        self.assertEqual(0, result['policies']['two_per_fixture']['blocked'])

    def test_correction_changes_outcome_and_drawdown_without_reopening_entry(self):
        place = START + timedelta(hours=1)
        corrected = [
            receipt('10', '20', '10', place + timedelta(hours=1), settled=place + timedelta(minutes=30)),
            receipt('10', '0', '-10', place + timedelta(hours=3), settled=place + timedelta(hours=2)),
        ]
        rows = [
            order('a', place, fixture='one', receipts=corrected),
            order('b', place + timedelta(days=1), fixture='two',
                  receipts=[settled(place + timedelta(days=1), stake='10', payout='5')]),
        ]
        result = exposure(document(rows))['policies']['all']
        self.assertEqual(Decimal('-15'), result['realized_units'])
        self.assertEqual(Decimal('15'), result['max_drawdown_units'])
        self.assertEqual(2, result['entries'])

    def test_entry_principal_is_not_inferred_from_receipt_stake(self):
        place = START + timedelta(hours=1)
        rows = [order('unknown', place, receipts=[settled(place, stake='50')])]
        result = exposure(document(rows))['policies']['all']
        self.assertEqual(1, result['unknown_entry_principal'])
        self.assertIsNone(result['known_principal_peak'])
        known = exposure(document([
            order('known', place, stake='5', receipts=[settled(place)]),
        ]))['policies']['all']
        self.assertEqual(0, known['unknown_entry_principal'])
        self.assertEqual(Decimal('5'), known['known_principal_peak'])

    def test_pending_position_stays_occupied_and_all_follow_keeps_both(self):
        place = START + timedelta(hours=1)
        rows = [
            order('pending', place, fixture='same'),
            order('next', place + timedelta(hours=1), fixture='same',
                  receipts=[settled(place, observed=place + timedelta(hours=2))]),
        ]
        result = exposure(document(rows))
        self.assertEqual(1, result['policies']['one_per_fixture']['blocked'])
        self.assertEqual(2, result['policies']['all']['entries'])
        self.assertEqual(2, result['policies']['all']['pending_entries'])
        self.assertEqual('first valid settled receipt observed strictly before the next entry',
                         result['release_rule'])


if __name__ == '__main__':
    unittest.main()
