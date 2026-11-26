import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from infra.profitability.experiments.competition_analysis import analyze

START = datetime(2026, 11, 1, tzinfo=timezone.utc)


def order(order_id, fixture, expert, outcome, *, market='total', settled=True):
    placed = START + timedelta(minutes=len(order_id))
    observed = placed + timedelta(minutes=2)
    stake = Decimal('10')
    net = Decimal(str(outcome))
    return {
        'order_id': order_id,
        'fixture_id': fixture,
        'expert_id': expert,
        'sport': 'football',
        'market_kind': market,
        'placed_at': placed.isoformat(),
        'signal_at': placed.isoformat(),
        'preflight_at': placed.isoformat(),
        'signal_price': '2',
        'preflight_price': '2',
        'overround': '0.03',
        'receipts': [{
            'observed_at': observed.isoformat(),
            'settled_at': observed.isoformat() if settled else None,
            'stake': str(stake),
            'payout': str(stake + net),
            'net': str(net),
            'fees': None,
            'currency': None,
        }],
    }


def cohort(rows):
    return {
        'begin': START.isoformat(),
        'end': (START + timedelta(days=1)).isoformat(),
        'cutoff': (START + timedelta(days=2)).isoformat(),
        'rows': rows,
    }


def metadata():
    return {'fixtures': [
        {'fixture_id': f'a{i}', 'competition_id': 'A', 'competition_name': 'League A', 'sport': 'football'}
        for i in range(1, 7)
    ] + [
        {'fixture_id': f'b{i}', 'competition_id': 'B', 'competition_name': 'League B', 'sport': 'football'}
        for i in range(1, 5)
    ]}


class CompetitionAnalysisTest(unittest.TestCase):
    def test_joins_leagues_counts_unresolved_and_bootstraps_whole_fixtures(self):
        rows = [
            order('1', 'a1', 'expert-x', '1'),
            order('2', 'a1', 'expert-y', '-1', market='handicap'),
            order('3', 'a2', 'expert-x', '-1'),
            order('4', 'a3', 'expert-y', '1'),
            order('5', 'a4', 'expert-x', '-1'),
            order('6', 'a5', 'expert-y', '0'),
            order('7', 'a6', 'expert-x', '-1', settled=False),
            order('8', 'b1', 'expert-x', '1'),
            order('9', 'b2', 'expert-x', '-1'),
            order('10', 'b3', 'expert-x', '1'),
            order('11', 'b4', 'expert-x', '-1'),
            order('12', 'orphan', 'expert-z', '-1'),
        ]
        result = analyze(cohort(rows), metadata(), bootstrap_rounds=200)
        repeated = analyze(cohort(rows), metadata(), bootstrap_rounds=200)
        self.assertEqual(result, repeated)
        self.assertEqual(12, result['coverage']['accepted_orders'])
        self.assertEqual(11, result['coverage']['settled_orders'])
        self.assertEqual(1, result['coverage']['unresolved_orders'])
        self.assertEqual(1, result['coverage']['unmatched_orders'])
        self.assertEqual(2, result['coverage']['competition_groups'])
        by_name = {item['competition_name']: item for item in result['competitions']}
        league_a = by_name['League A']
        self.assertEqual((7, 6, 1, 5), (
            league_a['accepted_orders'], league_a['settled_orders'],
            league_a['unresolved_orders'], league_a['fixtures'],
        ))
        self.assertIsNotNone(league_a['fixture_cluster_intervals']['fixture_cluster_95'])
        self.assertEqual(1, league_a['same_expert_comparison']['paired_experts'])
        self.assertEqual('not_assessed_from_returns', result['integrity_assessment'])

    def test_conflicting_fixture_metadata_is_rejected(self):
        data = {'fixtures': [
            {'fixture_id': 'a1', 'competition_id': 'A', 'competition_name': 'League A', 'sport': 'football'},
            {'fixture_id': 'a1', 'competition_id': 'B', 'competition_name': 'League B', 'sport': 'football'},
        ]}
        with self.assertRaisesRegex(ValueError, 'conflicting competition metadata'):
            analyze(cohort([order('1', 'a1', 'expert-x', '1')]), data, bootstrap_rounds=100)

    def test_duplicate_orders_are_rejected(self):
        row = order('same', 'a1', 'expert-x', '1')
        with self.assertRaisesRegex(ValueError, 'duplicate order_id'):
            analyze(cohort([row, row]), metadata(), bootstrap_rounds=100)

    def test_small_competition_does_not_get_a_fixture_interval(self):
        rows = [order(str(i), f'b{i}', 'expert-x', '1') for i in range(1, 5)]
        result = analyze(cohort(rows), metadata(), bootstrap_rounds=100)
        league_b = next(row for row in result['competitions'] if row['competition_name'] == 'League B')
        self.assertEqual(4, league_b['settled_orders'])
        self.assertIsNone(league_b['fixture_cluster_intervals'])


if __name__ == '__main__':
    unittest.main()
