from datetime import datetime, timezone
from decimal import Decimal
import json
import unittest

from replay_opposite import observed_returns, summarize


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
