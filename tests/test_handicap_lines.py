from decimal import Decimal
import unittest

from app.handicap_lines import line_projection, line_terms
from app.settlement import settle
from app.review_tool_evidence import snapshot_result
from app.review_summary import make_summary


class NativeHandicapTests(unittest.TestCase):
    def test_capture_home_hv_and_away_option_have_opposite_lines(self):
        for market in ('4', '19'):
            for raw in ('1', '.25', '-.5', '-1.75', '0'):
                for side in ('home', 'away'):
                    quote = dict(fixture_id='leyu:360', market_id=market, market_kind='handicap',
                                 selection=side, line=raw, selection_id='native-option')
                    with self.subTest(market=market, raw=raw, side=side):
                        value = line_projection(quote)
                        expected = Decimal(raw) * (-1 if side == 'away' else 1)
                        self.assertEqual(expected, Decimal(value['selection_line']))
                        self.assertEqual('provider_home', value['line_basis'])
                        self.assertEqual(raw, value['line'])
                        self.assertEqual('native-option', value['selection_id'])
                        self.assertNotIn('selection_line', quote)

    def test_other_sources_and_markets_do_not_flip(self):
        base = dict(fixture_id='leyu:360', market_id='4', market_kind='handicap', selection='away', line='1')
        for change in ({'fixture_id':'other:360'}, {'fixture_id':'leyu:unverified'},
                       {'market_id':'39'}, {'market_kind':'total'}):
            terms = line_terms(base | change)
            self.assertEqual({'line_basis':'selection', 'selection_line':'1'}, terms)

    def test_away_minus_one_at_one_goal_margin_is_push(self):
        signal = dict(kind='handicap', selection='away', line='1', price='1.74',
                      period='full_time', phase='scheduled', line_basis='provider_home')
        result = dict(status='confirmed', home=1, away=2)
        value = settle(signal, result)
        self.assertEqual(('push', Decimal('0')), (value.outcome, value.unit_net))
        self.assertEqual('1', signal['line'])
        self.assertEqual('win', settle(signal | {'line_basis':'selection'}, result).outcome)

    def test_native_home_and_away_match_explicit_selected_line_across_scores(self):
        for raw in ('-1.75', '-.5', '0', '.25', '1'):
            for side in ('home', 'away'):
                for home, away in ((0, 0), (1, 0), (1, 2), (3, 1)):
                    signal = dict(kind='handicap', selection=side, line=raw, price='1.9',
                                  phase='scheduled', period='full_time', line_basis='provider_home')
                    result = dict(status='confirmed', home=home, away=away)
                    selected = Decimal(raw) * (-1 if side == 'away' else 1)
                    actual = settle(signal, result)
                    expected = settle(signal | {'line_basis':'selection', 'line':selected}, result)
                    self.assertEqual((expected.outcome, expected.unit_net, expected.effective_stake),
                                     (actual.outcome, actual.unit_net, actual.effective_stake))

    def test_unrecognised_or_inapplicable_basis_stays_pending(self):
        for kind, basis in (('handicap','unknown'), ('total','provider_home')):
            value = settle(dict(kind=kind, line='1', selection='over', price='2',
                                phase='scheduled', line_basis=basis),
                           dict(status='confirmed', home=2, away=1))
            self.assertEqual('pending', value.outcome)

    def test_model_and_receipt_declare_selected_line_without_rewriting_raw_line(self):
        quote = dict(fixture_id='leyu:360', market_id='4', market_kind='handicap',
                     selection='away', line='1', period='full_time', quote_id='native-quote')
        original = {'fixture':{'id':'leyu:360'}, 'markets':[quote], 'refresh':{'attempted':False}}
        wire = snapshot_result('mcp_list_match_markets', original, compact_markets=True)
        self.assertEqual('1', wire['markets'][0]['line'])
        self.assertEqual('-1', wire['markets'][0]['selection_line'])
        self.assertEqual('provider_home', wire['markets'][0]['line_basis'])
        self.assertNotIn('selection_line', snapshot_result('mcp_list_match_markets', original)['markets'][0])
        receipt = make_summary([quote | {'recommendation_price':'1.74'}], accepted_total=1,
                               rejected=[], review_mode='real_time')
        self.assertEqual('-1', receipt['items'][0]['selection_line'])
        self.assertIn('盘口线 -1，方向 客队', receipt['content'])
        self.assertEqual('1', quote['line'])
