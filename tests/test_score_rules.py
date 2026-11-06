from copy import deepcopy
from decimal import Decimal
import unittest

from app.score_rules import LEYU_PERIOD_TOTAL_RULE, settlement_rule
from app.settlement import settle


class NativeTotalRuleTests(unittest.TestCase):
    def signal(self):
        return dict(market_id='2', market_kind='total', period='full_time',
                    phase='live', score_basis='unknown')

    def test_verified_markets_have_auditable_period_rule_without_mutating_snapshots(self):
        fixture = {'id': 'leyu:5721572', 'sport': 'football'}
        for market, period in (('2', 'full_time'), ('18', 'first_half')):
            row = self.signal() | {'market_id': market, 'period': period}
            original = deepcopy(row)
            rule = settlement_rule(fixture, row)
            self.assertEqual({'reference': LEYU_PERIOD_TOTAL_RULE, 'score_basis': 'full_period',
                              'provider': 'leyu', 'market_id': market, 'period': period}, rule)
            self.assertEqual(original, row)

    def test_rule_is_not_generalized_to_unverified_provider_sport_market_or_period(self):
        fixture = {'id': 'leyu:5721572', 'sport': 'football'}
        for changed in ({'id': 'other:5721572'}, {'id': 'leyu:fixture'}, {'id': 'leyu:１２３'},
                        {'id': 'leyu:123\n'}, {'sport': 'basketball'}):
            self.assertIsNone(settlement_rule(fixture | changed, self.signal()))
        for changed in ({'market_id': '4'}, {'market_id': '38'}, {'market_id': 'total'},
                        {'period': 'first_half'}, {'period': 'second_half'}, {'phase': 'scheduled'},
                        {'market_kind': 'handicap'}, {'score_basis': 'full_period'},
                        {'score_basis': 'remaining_period'}, {'score_basis': 'invalid'},
                        {'score_basis': None}):
            self.assertIsNone(settlement_rule(fixture, self.signal() | changed))

    def test_native_nonzero_baseline_receipts_distinguish_full_and_remaining_scores(self):
        # E330 native ordinary-history projection; no account/order identity or price.
        # 3 full-time half-losses and the first-half Over/Under opposing pair.
        samples = [
            ('2', 'full_time', 'over', '2.25', (1, 0), (2, 0), 'half_loss'),
            ('2', 'full_time', 'over', '3.25', (1, 0), (2, 1), 'half_loss'),
            ('2', 'full_time', 'over', '2.25', (0, 1), (1, 1), 'half_loss'),
            ('18', 'first_half', 'over', '1.5', (0, 1), (0, 2), 'win'),
            ('18', 'first_half', 'under', '1.5', (0, 1), (0, 2), 'loss'),
        ]
        for market, period, side, line, baseline, score, expected in samples:
            with self.subTest(market=market, side=side, line=line):
                row = self.signal() | {'market_id': market, 'period': period}
                rule = settlement_rule({'id': 'leyu:5721572', 'sport': 'football'}, row)
                before = dict(home=baseline[0], away=baseline[1])
                result = dict(status='confirmed', home=score[0], away=score[1])
                if period == 'first_half':
                    before.update(half_home=baseline[0], half_away=baseline[1])
                    result.update(half_home=score[0], half_away=score[1])
                signal = dict(kind='total', selection=side, line=line, price='1.90',
                              period=period, phase='live', score_baseline=before)
                actual = settle(signal | {'score_basis': rule['score_basis']}, result)
                self.assertEqual(expected, actual.outcome)
                self.assertNotEqual(expected, settle(signal | {'score_basis': 'remaining_period'}, result).outcome)
                if expected == 'half_loss':
                    self.assertEqual((Decimal('-.5'), Decimal('.5')), (actual.unit_net, actual.effective_stake))

    def test_confirmed_rule_still_requires_valid_price_line_and_official_period_result(self):
        signal = dict(kind='total', selection='over', line='1.5', price='1.9',
                      phase='live', period='first_half', score_basis='full_period')
        result = dict(status='confirmed', home=0, away=2, half_home=0, half_away=2)
        for change in ({'status': 'provisional'}, {'half_home': None}, {'half_away': 3}):
            self.assertEqual('pending', settle(signal, result | change).outcome)
        for change in ({'price': 'NaN'}, {'line': '.1'}, {'selection': 'invalid'}):
            self.assertEqual('pending', settle(signal | change, result).outcome)
        self.assertEqual('void', settle(signal, {'status': 'void'}).outcome)
