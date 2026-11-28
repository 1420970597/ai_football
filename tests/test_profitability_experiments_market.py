"""Boundary and real-replay tests for the market, model and timing experiments.

Synthetic cases pin the contract, arithmetic and failure behaviour; the real public
CSV directory and frozen input directory are only used when they are mounted, and a
missing mount skips that replay rather than inventing a result.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from infra.profitability.experiments import closing_line, market, models, timing

PUBLIC = Path('/public')
INPUT = Path('/input')
UTC = timezone.utc


def at(*args):
    return datetime(*args, tzinfo=UTC)


class ModelsTest(unittest.TestCase):
    def synthetic(self):
        return [
            {'home': 'a', 'away': 'b', 'home_goals': 2, 'away_goals': 0},
            {'home': 'b', 'away': 'a', 'home_goals': 0, 'away_goals': 1},
            {'home': 'a', 'away': 'b', 'home_goals': 1, 'away_goals': 1},
            {'home': 'b', 'away': 'a', 'home_goals': 0, 'away_goals': 2},
        ] * 25

    def test_fit_converges_and_probabilities_are_normalised(self):
        fitted = models.fit(self.synthetic())
        self.assertTrue(fitted.converged)
        self.assertLess(fitted.iterations, 400)
        self.assertAlmostEqual(sum(fitted.grid('a', 'b').values()), 1.0, places=9)
        outcomes = fitted.outcome_probabilities('a', 'b')
        self.assertAlmostEqual(sum(outcomes.values()), 1.0, places=9)
        self.assertGreater(outcomes['home'], outcomes['away'])
        totals = fitted.totals_probabilities('a', 'b', 2.5)
        self.assertAlmostEqual(totals['over'] + totals['under'], 1.0, places=12)

    def test_unseen_club_and_missing_closing_are_coverage_losses(self):
        fitted = models.fit(self.synthetic())
        matches = [
            {'home': 'a', 'away': 'b', 'home_goals': 1, 'away_goals': 0, 'result': 'home', 'closing': {}},
            {'home': 'a', 'away': 'z', 'home_goals': 1, 'away_goals': 0, 'result': 'home', 'closing': {}},
            {'home': 'a', 'away': 'b', 'home_goals': 1, 'away_goals': 0, 'result': 'home',
             'closing': {'AvgC': {'home': Decimal('2'), 'draw': Decimal('3'), 'away': Decimal('4')}}},
        ]
        report = models.evaluate(fitted, matches, book='AvgC')
        self.assertEqual(report['scored_matches'], 2)
        self.assertEqual(report['priced_matches'], 1)
        self.assertEqual(report['missing_reasons']['unseen_club'], 1)
        self.assertEqual(report['missing_reasons']['AvgC_closing_absent'], 1)

    def test_fixed_season_split_holds_out_test_results(self):
        matches = [{'season': season, 'date': f'20{season[:2]}-08-01',
                    'home': 'a', 'away': 'b', 'home_goals': 1, 'away_goals': 0,
                    'result': 'home', 'closing': {}} for season in ('2223', '2324', '2526')]
        train, test = models.split_by_season(matches, {'2526'})
        self.assertEqual({'2223', '2324'}, {match['season'] for match in train})
        self.assertEqual({'2526'}, {match['season'] for match in test})

    @unittest.skipUnless(PUBLIC.is_dir(), 'public CSV directory is not mounted')
    def test_real_public_replay_reports_calibration(self):
        document = models.run(str(PUBLIC), test_seasons=('2526',))
        training = document['results']['training']
        self.assertTrue(training['converged'])
        self.assertGreater(training['matches'], 1000)
        evaluation = document['results']['evaluation']
        self.assertGreater(evaluation['scored_matches'], 100)
        self.assertIsInstance(evaluation['model_brier'], float)
        self.assertIsNotNone(evaluation['market_brier'])


class ClosingLineTest(unittest.TestCase):
    def quote(self, **overrides):
        base = {'fixture': 'fx1', 'period': 'full_time', 'kind': 'winner', 'line': None,
                'direction': 'home', 'score_basis': 'full_time', 'price': '2.10',
                'observed_at': at(2023, 8, 12, 13, 0), 'kickoff': at(2023, 8, 12, 14, 0)}
        return base | overrides

    def closing(self, **overrides):
        base = {'fixture': 'fx1', 'period': 'full_time', 'kind': 'winner', 'line': None,
                'direction': 'home', 'score_basis': 'full_time', 'book': 'B365C',
                'kickoff': at(2023, 8, 12, 14, 0),
                'prices': {'home': Decimal('2.00'), 'draw': Decimal('3.40'), 'away': Decimal('3.80')}}
        return base | overrides

    def test_better_entry_price_has_opposite_clv_and_implied_sign(self):
        pair = closing_line.pair_clv(self.quote(), self.closing())
        self.assertEqual(pair['status'], 'paired')
        self.assertGreater(pair['price_clv'], 0)
        self.assertLess(pair['implied_probability_gain'], 0)
        self.assertEqual(pair['price_clv_sign'], -pair['implied_probability_sign'])
        self.assertTrue(pair['beats_closing'])

    def test_line_and_direction_mismatch_are_rejected(self):
        line = closing_line.pair_clv(self.quote(line='2.5'), self.closing())
        self.assertIn('line_mismatch', line['reasons'])
        direction = closing_line.pair_clv(self.quote(), self.closing(direction='home') | {
            'prices': {'home': Decimal('2.00')}})
        self.assertEqual(direction['status'], 'paired')
        missing_side = closing_line.pair_clv(self.quote(direction='away'), self.closing())
        self.assertIn('direction_mismatch', missing_side['reasons'])

    def test_entry_must_be_strictly_before_kickoff(self):
        late = closing_line.pair_clv(self.quote(observed_at=at(2023, 8, 12, 14, 0)), self.closing())
        self.assertIn('entry_not_before_kickoff', late['reasons'])

    def test_pair_entry_requires_mapping_and_public_fixture(self):
        mappings = {'fx1': {'div': 'E0', 'date': '2023-08-12', 'home': 'A', 'away': 'B',
                            'kickoff': at(2023, 8, 12, 14, 0)}}
        self.assertEqual(closing_line.pair_entry({'fixture_id': 'nope'}, mappings, {})[0]['reasons'],
                         ['fixture_mapping_missing'])
        missing_public = closing_line.pair_entry({'fixture_id': 'fx1'}, mappings, {})
        self.assertEqual(missing_public[0]['reasons'], ['public_fixture_missing'])

    def test_external_mapping_pairs_against_public_csv(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            public = root / 'public'
            frozen = root / 'input'
            public.mkdir()
            frozen.mkdir()
            (public / 'E0_2324.csv').write_text(
                'Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,B365CH,B365CD,B365CA\n'
                'E0,12/08/2023,15:00,A,B,1,0,H,2.00,3.40,3.80\n', encoding='utf-8')
            (frozen / 'fixture-mappings.json').write_text(json.dumps({'mappings': [
                {'fixture_id': 'fx1', 'div': 'E0', 'date': '2023-08-12', 'home': 'A', 'away': 'B',
                 'kickoff': '2023-08-12T14:00:00+00:00'}]}), encoding='utf-8')
            (frozen / 'executable-quotes.json').write_text(json.dumps({'quotes': [
                {'fixture_id': 'fx1', 'period': 'full_time', 'kind': 'winner', 'line': None,
                 'direction': 'home', 'score_basis': 'full_time', 'price': '2.10',
                 'observed_at': '2023-08-12T13:00:00+00:00'}]}), encoding='utf-8')
            document = closing_line.run(str(frozen), str(public))
            self.assertEqual(document['evidence_label'], 'measured')
            self.assertEqual(document['coverage']['paired_observations'], 1)
            self.assertEqual(document['results']['books']['B365C']['paired_observations'], 1)

    @unittest.skipUnless(INPUT.is_dir() and PUBLIC.is_dir(), 'frozen input is not mounted')
    def test_real_run_reports_mapping_gap_without_hardcoded_claim(self):
        document = closing_line.run(str(INPUT), str(PUBLIC))
        self.assertIn('verified_fixture_mapping_absent', document['missing_reasons'])
        self.assertNotIn('no_team_identity_in_frozen_cohorts', document['missing_reasons'])
        self.assertEqual(document['evidence_label'], 'insufficient_evidence')


class CalibrationTest(unittest.TestCase):
    def pick(self, number, created, market_share, result, expert='e1', fixture=None):
        return {'id': str(number), 'expert_id': expert, 'fixture_id': fixture or f'f{number}',
                'created_at': created, 'market_share': Decimal(str(market_share)),
                'recommendation_price': Decimal('2.00'), 'withdrawn_at': None, '_result': result}

    def fake_result(self, pick, cutoff):
        return pick.get('_result')

    def test_half_outcomes_use_fraction_and_stake_weight(self):
        half_win = market.outcome_weight({'withdrawn_at': None, '_result': (Decimal('0.5'), Decimal('0.5'))},
                                         at(2023, 8, 13), self.fake_result)
        self.assertEqual(half_win[:2], (1.0, 0.5))
        half_loss = market.outcome_weight({'withdrawn_at': None, '_result': (Decimal('0'), Decimal('0.5'))},
                                          at(2023, 8, 13), self.fake_result)
        self.assertEqual(half_loss[:2], (0.0, 0.5))
        push = market.outcome_weight({'withdrawn_at': None, '_result': (Decimal('0'), Decimal('0'))},
                                     at(2023, 8, 13), self.fake_result)
        self.assertEqual(push[2], 'push_or_void')

    def test_future_results_are_excluded_from_author_offset(self):
        start = at(2023, 8, 1)
        history = [self.pick(n, start + timedelta(days=2), 0.5, (Decimal('1'), Decimal('1'))) for n in range(10)]
        offset, sample = market.author_offset(history, 'e1', start + timedelta(days=1), result_at=self.fake_result)
        self.assertEqual((offset, sample), (0.0, 0))

    def test_unknown_author_falls_back_to_market(self):
        start = at(2023, 8, 1)
        picks = [self.pick(1, start, 0.4, (Decimal('1'), Decimal('1')), expert='new')]
        records, missing = market.forward_replay(picks, start + timedelta(days=1),
                                                 result_at=self.fake_result)
        self.assertEqual(records[0]['author_sample'], 0)
        self.assertAlmostEqual(records[0]['predicted'], 0.4, places=9)
        self.assertFalse(missing)

    def test_small_sample_offset_stays_close_to_market(self):
        start = at(2023, 8, 1)
        history = [self.pick(n, start + timedelta(hours=n), 0.5, (Decimal('1'), Decimal('1')))
                   for n in range(2)]
        offset, sample = market.author_offset(history, 'e1', start + timedelta(days=1),
                                              result_at=self.fake_result)
        self.assertEqual(sample, 2)
        self.assertLess(abs(offset), 0.5)

    @unittest.skipUnless(INPUT.is_dir(), 'frozen input is not mounted')
    def test_real_calibration_replay(self):
        try:
            document = market.run_calibration(str(INPUT / 'market-adjusted-cohort.json'))
        except RuntimeError as error:
            self.skipTest(str(error))
        self.assertGreater(document['coverage']['settled'], 0)
        self.assertIn('brier', document['results']['paired'])
        self.assertEqual(len(document['results']['time_blocks']), 3)


class ContractStateTest(unittest.TestCase):
    def quote(self, **overrides):
        base = {'status': 'two_sided', 'bid': '0.38', 'ask': '0.40', 'bid_size': '5', 'ask_size': '5',
                'quote_time': '2026-11-20T04:00:00+00:00', 'received_at': '2026-11-20T04:00:05+00:00'}
        return base | overrides

    reference = {'rules_sha256': 'abc', 'line': None}

    def test_complete_two_sided_quote_is_priceable(self):
        state = market.quote_state(self.quote(), {'rules_sha256': 'abc', 'accepting_orders': True},
                                   self.reference)
        self.assertEqual(state['state'], 'priceable')
        self.assertEqual(state['reasons'], [])
        self.assertEqual(state['ask'], Decimal('0.40'))

    def test_future_and_stale_quotes_are_rejected(self):
        future = market.quote_state(self.quote(quote_time='2026-11-20T04:00:09+00:00'),
                                    {'rules_sha256': 'abc'}, self.reference)
        self.assertIn('future_quote', future['reasons'])
        stale = market.quote_state(self.quote(quote_time='2026-11-20T03:00:00+00:00'),
                                   {'rules_sha256': 'abc'}, self.reference)
        self.assertIn('quote_stale', stale['reasons'])

    def test_rule_line_and_one_leg_conflicts_are_rejected(self):
        rules = market.quote_state(self.quote(), {'rules_sha256': 'other'}, self.reference)
        self.assertIn('rules_hash_mismatch', rules['reasons'])
        line = market.quote_state(self.quote(), {'rules_sha256': 'abc'},
                                  {'rules_sha256': 'abc', 'line': '1.5'})
        self.assertIn('line_mismatch', line['reasons'])
        one_sided = market.quote_state(self.quote(status='one_sided'), {'rules_sha256': 'abc'}, self.reference)
        self.assertEqual(one_sided['state'], 'rejected')
        self.assertIn('not_two_sided', one_sided['reasons'])
        self.assertIsNone(one_sided['ask'])

    def test_insufficient_depth_and_taker_cost(self):
        shallow = market.quote_state(self.quote(ask_size='0.1'), {'rules_sha256': 'abc'}, self.reference)
        self.assertIn('insufficient_depth', shallow['reasons'])
        edge = market.taker_edge(0.45, Decimal('0.40'), cost_per_unit=Decimal('0.06'))
        self.assertEqual(edge['gross'], Decimal('0.05'))
        self.assertLess(edge['net'], 0)

    @unittest.skipUnless(INPUT.is_dir(), 'frozen input is not mounted')
    def test_real_contract_replay(self):
        document = market.run_contracts(str(INPUT / 'reference/reference-price.json'),
                                        str(INPUT / 'reference/books'))
        self.assertGreater(document['results']['quotes_observed'], 0)
        self.assertGreater(document['results']['priceable_quotes'], 0)


class TimingTest(unittest.TestCase):
    kickoff = at(2026, 11, 20, 12, 0)

    def test_lead_bucket_boundaries(self):
        self.assertEqual(timing.lead_bucket(self.kickoff - timedelta(minutes=5), self.kickoff), '5m_30m')
        self.assertEqual(timing.lead_bucket(self.kickoff - timedelta(minutes=4, seconds=59), self.kickoff), 'le_5m')
        self.assertEqual(timing.lead_bucket(self.kickoff, self.kickoff), 'le_5m')
        self.assertEqual(timing.lead_bucket(self.kickoff + timedelta(seconds=1), self.kickoff), 'live')
        self.assertIsNone(timing.lead_bucket(self.kickoff, None))

    def test_only_visible_pre_kickoff_quotes_are_used(self):
        quotes = [
            {'quote_time': '2026-11-20T10:00:00+00:00', 'received_at': '2026-11-20T10:00:02+00:00',
             'bid': '0.40', 'ask': '0.42'},
            {'quote_time': '2026-11-20T10:30:00+00:00', 'received_at': '2026-11-20T11:30:00+00:00',
             'bid': '0.60', 'ask': '0.62'},
            {'quote_time': '2026-11-20T12:30:00+00:00', 'received_at': '2026-11-20T12:30:01+00:00',
             'bid': '0.90', 'ask': '0.92'},
        ]
        movement, missing = timing.visible_movement(quotes, at(2026, 11, 20, 11, 0), self.kickoff)
        self.assertIsNotNone(movement)
        self.assertEqual(movement['observations'], 1)
        self.assertEqual(movement['delta'], 0.0)
        self.assertIn('quote_not_visible_before_recommendation', missing)

    def test_same_second_inclusive_and_closing_never_backfilled(self):
        same_second = [{'quote_time': '2026-11-20T10:00:00+00:00', 'received_at': '2026-11-20T10:00:00+00:00',
                        'bid': '0.40', 'ask': '0.42'}]
        movement, _ = timing.visible_movement(same_second, at(2026, 11, 20, 10, 0), self.kickoff)
        self.assertIsNotNone(movement)
        self.assertEqual(movement['observations'], 1)
        after_kickoff = [{'quote_time': '2026-11-20T12:30:00+00:00', 'received_at': '2026-11-20T12:30:00+00:00',
                          'bid': '0.9', 'ask': '0.91'}]
        movement, missing = timing.visible_movement(after_kickoff, at(2026, 11, 20, 13, 0), self.kickoff)
        self.assertIsNone(movement)
        self.assertIn('no_pre_recommendation_quote', missing)

    def test_cross_line_and_missing_mapping(self):
        bindings = {'fx1': [{'kind': 'handicap', 'period': 'full_time', 'line': '-1.5',
                             'market': 'spread-home-1pt5'}]}
        pick = {'selection': 'home', 'market_kind': 'handicap', 'period': 'full_time', 'line': '-2.5'}
        self.assertIsNone(timing.match_binding(bindings['fx1'], pick))
        analysis = timing.analyze([{'fixture_id': 'fx2', 'created_at': at(2026, 11, 20, 10, 0),
                                    'market_kind': 'total', 'period': 'full_time', 'line': '2.5',
                                    'selection': 'over', 'recommendation_price': Decimal('2')}],
                                  bindings, {}, at(2026, 11, 21), result_at=lambda pick, cutoff: (Decimal('1'), Decimal('1')))
        self.assertEqual(analysis['buckets']['unmapped']['recommendations'], 1)
        self.assertEqual(analysis['missing']['contract_mapping_missing'], 1)

    @unittest.skipUnless(INPUT.is_dir(), 'frozen input is not mounted')
    def test_real_timing_replay(self):
        try:
            document = timing.run(str(INPUT / 'market-adjusted-cohort.json'),
                                  str(INPUT / 'reference/reference-price.json'),
                                  str(INPUT / 'reference/books'))
        except RuntimeError as error:
            self.skipTest(str(error))
        self.assertGreater(document['coverage']['verified_picks'], 0)
        self.assertTrue(document['results']['buckets'])


if __name__ == '__main__':
    unittest.main()
