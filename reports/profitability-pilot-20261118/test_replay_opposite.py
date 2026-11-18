from datetime import datetime, timezone
from decimal import Decimal
import json
import unittest

from replay_opposite import observed_returns, summarize, summarize_calibration


def team_total():
    return {
        'created_at': datetime(2026, 1, 1, tzinfo=timezone.utc),
        'fixture_id': 'leyu:1', 'phase': 'scheduled', 'sport': 'football',
        'market_kind': 'team_total', 'market_id': '11', 'period': 'full_time',
        'selection': 'over', 'recommendation_price': Decimal('2.00'),
        'settlement_valid': True, 'outcome': 'half_win', 'unit_net': Decimal('0.50'),
        'effective_stake': Decimal('0.50'),
        'evidence': {
            'team_side': 'away',
            'market_snapshot': {
                'play_id': '11', 'market_instance_id': 'instance',
                'options': [
                    {'selection': 'over', 'selection_id': '1', 'price': '2.00'},
                    {'selection': 'under', 'selection_id': '2', 'price': '1.90'},
                ],
            },
        },
    }


class OppositeReplayTests(unittest.TestCase):
    def test_requires_native_team_and_market_identity(self):
        row = team_total()
        self.assertEqual(observed_returns(row), (
            'comparable', (Decimal('0.50'), Decimal('-0.50'), Decimal(0), Decimal('0.50')),
        ))
        for changes in ({'team_side': 'home'}, {'play_id': '10'}, {'market_instance_id': ''}):
            with self.subTest(changes=changes):
                row = team_total()
                property_owner = row['evidence'] if 'team_side' in changes else row['evidence']['market_snapshot']
                property_owner.update(changes)
                self.assertEqual(observed_returns(row), ('team_contract_unverified', None))
        row = team_total()
        del row['evidence']['market_snapshot']
        self.assertEqual(observed_returns(row), ('two_sided_snapshot_unavailable', None))

    def test_contract_summary_preserves_row_and_counts_unique_fixtures(self):
        row = team_total()
        row['evidence'] = json.dumps(row['evidence'])
        rows = [row, row]
        phase_groups = summarize(rows, row['created_at'])
        contract_groups = summarize(rows, row['created_at'], by_contract=True)
        self.assertEqual([g['phase'] for g in phase_groups], ['all', 'scheduled'])
        self.assertEqual(contract_groups, [{
            'window': 'later', 'phase': 'scheduled', 'sport': 'football', 'market_kind': 'team_total',
            'period': 'full_time', 'comparable_fixtures': 1, 'comparable': 2, 'follow_units': Decimal('1.00'),
            'opposite_units': Decimal('-1.00'), 'paired_units': Decimal(0), 'effective_stake': Decimal('1.00'),
        }])

    def test_calibration_uses_equal_fixture_weights_and_half_stakes(self):
        row = dict(team_total(), confidence=Decimal('0.5'))
        row['evidence']['market_snapshot']['options'][1]['price'] = '2.00'
        row['evidence'] = json.dumps(row['evidence'])
        lost = dict(row, fixture_id='leyu:2', outcome='loss', effective_stake=Decimal(1))
        push = dict(row, outcome='push', effective_stake=Decimal(0))
        inconsistent = dict(row, outcome='loss', effective_stake=Decimal('0.5'))
        result = summarize_calibration([row, row, row, lost, push, inconsistent], row['created_at'])
        group = next(group for group in result['groups'] if group['phase'] == 'all')
        self.assertEqual(group['scored'], 4)
        self.assertEqual(group['settled_fixtures'], 2)
        self.assertEqual(group['observed_win_share'], Decimal('0.5'))
        self.assertEqual(group['market_brier'], Decimal('0.25'))
        self.assertEqual(group['no_active_stake'], 1)
        self.assertEqual(group['settlement_stake_inconsistent'], 1)
        self.assertEqual(result['review_confidence_distribution'][0]['winning_stake'], Decimal('1.5'))

    def test_three_way_complete_market_uses_one_third_neutral_chance(self):
        row = dict(team_total(), market_kind='winner', market_id='1', selection='draw',
                   recommendation_price=Decimal(4), effective_stake=Decimal(1),
                   outcome='win', confidence=Decimal('0.5'))
        prices = {'home': '2', 'draw': '4', 'away': '4'}
        row['evidence'] = json.dumps({'market_snapshot': {'options': [
            {'selection': side, 'selection_id': side, 'price': price} for side, price in prices.items()
        ]}})
        group = summarize_calibration([row], row['created_at'])['groups'][0]
        self.assertEqual(group['market_win_share'], Decimal('0.25'))
        self.assertEqual(group['market_brier'], Decimal('0.5625'))
        self.assertAlmostEqual(group['neutral_brier'], Decimal(4) / 9)
