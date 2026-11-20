from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from market_adjusted_experts import past_residual, prepare, replay, result_at


BEGIN = datetime(2026, 11, 1, tzinfo=timezone.utc)


def version(number, at, *, status='confirmed', outcome='win', stake='1', net='1'):
    return {'version': number, 'status': status, 'observed_at': at.isoformat(),
            'records': [{'computed_at': (at + timedelta(seconds=1)).isoformat(),
                         'outcome': outcome, 'stake': stake, 'net': net}]}


def signal(number, *, day=None, fixture=None, sport='football'):
    created = BEGIN + timedelta(days=day if day is not None else number)
    return {'id': str(number), 'fixture_id': fixture or f'leyu:{number}', 'expert_id': 'expert',
            'created_at': created.isoformat(), 'withdrawn_at': None, 'market_kind': 'total', 'market_id': '2',
            'period': 'full_time', 'selection': 'over', 'phase': 'scheduled', 'sport': sport,
            'recommendation_price': '2.00', 'native_acceptance': False,
            'evidence': {'market_snapshot': {'options': [
                {'selection': 'over', 'selection_id': 'a', 'price': '2'},
                {'selection': 'under', 'selection_id': 'b', 'price': '1.8'},
            ]}},
            'versions': [version(1, created + timedelta(hours=8))]}


class MarketAdjustedExpertsTest(unittest.TestCase):
    def test_revision_and_calculation_are_only_visible_as_of_cutoff(self):
        past = signal(1)
        change = BEGIN + timedelta(days=2)
        past['versions'].append(version(2, change, status='provisional'))
        past['versions'].append(version(3, change + timedelta(hours=1), outcome='half_loss', stake='.5', net='-.5'))
        _, pick = prepare(past)
        self.assertEqual((1, 1), result_at(pick, change))
        self.assertIsNone(result_at(pick, change + timedelta(minutes=30)))
        self.assertIsNone(result_at(pick, change + timedelta(hours=1, microseconds=5)))
        self.assertEqual((0, Decimal('.5')), result_at(pick, change + timedelta(hours=2)))

    def test_history_is_fixture_weighted_and_excludes_current_fixture_and_withdrawn(self):
        candidates = [
            signal(0, fixture='leyu:501'),
            signal(1, fixture='leyu:501'),
            signal(2, fixture='leyu:502'),
            signal(3, fixture='leyu:503') | {'withdrawn_at': (BEGIN + timedelta(days=4)).isoformat()},
            signal(4, fixture='leyu:999'),
        ]
        _, current = prepare(signal(5, fixture='leyu:999'))
        history = [prepare(row)[1] for row in candidates]
        count, residual = past_residual(current, history)
        self.assertEqual(2, count)
        self.assertEqual((1 - current['market_share']) * 2, residual)

    def test_rules_use_same_quotes_and_report_missing_acceptance(self):
        rows = [signal(n) for n in range(8)]
        rows[7]['versions'] = []
        end = BEGIN + timedelta(days=9)
        document = {'begin': BEGIN.isoformat(), 'end': end.isoformat(), 'cutoff': end.isoformat(),
                    'rows': list(reversed(rows))}
        result = replay(document, BEGIN + timedelta(days=6))
        self.assertFalse(result['validation_eligible'])
        earlier = result['cohorts']['earlier']
        later = result['cohorts']['later']
        self.assertEqual(1, earlier['market_adjusted']['settled'])
        self.assertEqual(2, later['market_adjusted']['selected'])
        self.assertEqual(1, later['market_adjusted']['settled_without_native_acceptance'])
        self.assertEqual(1, later['market_adjusted']['settlement_coverage']['missing_as_of_settlement'])
        self.assertEqual(0, later['market_adjusted_conservative']['selected'])
        self.assertEqual(Decimal('.98'), later['market_adjusted']['stressed_quote_units'])
        self.assertEqual(Decimal('.98'), later['market_adjusted']['stressed_quote_units_without_native_acceptance'])
        self.assertEqual(Decimal('.98'), later['market_adjusted']['stressed_quote_units_without_later_withdrawal'])
        with self.assertRaisesRegex(ValueError, 'duplicate recommendations'):
            replay(document | {'rows': [rows[0], rows[0]]}, BEGIN + timedelta(days=6))
