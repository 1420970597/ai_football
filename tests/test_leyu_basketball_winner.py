"""Native basketball play 37 from the authorized E2781 match-detail response."""
from datetime import datetime, timezone
import json
import unittest

import httpx

from services.collector.leyu import LeyuClient, DEFAULT_MATCH_ODDS_PATH
from services.collector.leyu_protocol import parse_quotes


def native_row():
    return {
        'mid': 'basketball-fixture', 'csid': '2', 'ms': 0, 'mmp': '0',
        'mgt': '1794304800000', 'mhn': '主队', 'man': '客队', 'tid': '208',
        'hpsPns': [{'chpid': '37', 'hpn': '全场独赢'}],
        'hpsData': [{'hps': [{
            'chpid': '37', 'hpid': '37', 'ctsp': '1794293313852',
            'hl': [{'hv': '', 'hid': 'native-winner', 'hs': 0, 'ol': [
                {'ot': '1', 'on': '主队', 'oid': 'home-option', 'os': 1, 'ov': 195000},
                {'ot': '2', 'on': '客队', 'oid': 'away-option', 'os': 1, 'ov': 185000},
            ]}],
        }], 'hpsAdd': [{'chpid': '37', 'hpid': '37', 'hl': []}]}],
    }


class BasketballWinnerTests(unittest.TestCase):
    def test_native_empty_line_preserves_two_exact_winner_options(self):
        quotes = parse_quotes(native_row(), 'leyu:basketball-fixture')
        self.assertEqual(['home', 'away'], [q['selection'] for q in quotes])
        self.assertEqual(['home-option', 'away-option'], [q['selection_id'] for q in quotes])
        self.assertEqual(['1.95', '1.85'], [q['price'] for q in quotes])
        for q in quotes:
            self.assertEqual(('37', '37', 'native-winner', 'winner', 'full_time', None),
                (q['market_id'], q['play_id'], q['market_instance_id'], q['market_kind'], q['period'], q['line']))
            self.assertEqual(('active', 'known', 'full_period', True),
                (q['status'], q['source_time_quality'], q['score_basis'], q['is_main']))

    def test_missing_null_and_zero_line_remain_no_handicap(self):
        for value in (None, '', '0', 0):
            with self.subTest(line=value):
                row = native_row()
                row['hpsData'][0]['hps'][0]['hl'][0]['hv'] = value
                quotes = parse_quotes(row, 'leyu:basketball-fixture')
                self.assertEqual(2, len(quotes))
                self.assertTrue(all(q['market_kind'] == 'winner' and q['line'] is None for q in quotes))

    def test_nonzero_line_cannot_turn_into_winner(self):
        row = native_row()
        row['hpsData'][0]['hps'][0]['hl'][0]['hv'] = '1.5'
        self.assertEqual([], parse_quotes(row, 'leyu:basketball-fixture'))

    def test_play_37_is_never_enabled_for_other_or_missing_sports(self):
        for sport in ('1', '5', '999', '', None):
            row = native_row()
            row['csid'] = sport
            self.assertFalse(any(q['market_kind'] == 'winner' for q in parse_quotes(row, 'leyu:basketball-fixture')))
            # Numeric unsupported rows remain suspended, never approved by a name.
            row['hpsData'][0]['hps'][0]['hl'][0]['hv'] = '0'
            quotes = parse_quotes(row, 'leyu:basketball-fixture')
            self.assertEqual(2, len(quotes))
            self.assertTrue(all(q['market_kind'] == 'unsupported' and q['status'] == 'suspended' for q in quotes))

    def test_native_status_and_unknown_time_are_not_repaired_by_mapping(self):
        for native_status, expected in ((1, 'suspended'), (2, 'closed'), (3, 'closed')):
            row = native_row()
            block = row['hpsData'][0]['hps'][0]
            block.pop('ctsp')
            block['hl'][0]['hs'] = native_status
            quotes = parse_quotes(row, 'leyu:basketball-fixture', datetime.now(timezone.utc))
            self.assertEqual(2, len(quotes))
            self.assertTrue(all(q['status'] == expected and q['source_updated_at'] is None
                and q['source_time_quality'] == 'unknown' for q in quotes))

    def test_live_score_basis_and_invalid_prices_remain_unavailable(self):
        row = native_row()
        row.update(ms=1, mmp='1')
        block = row['hpsData'][0]['hps'][0]
        block['hl'][0]['ol'][0]['ov'] = 'NaN'
        quotes = parse_quotes(row, 'leyu:basketball-fixture')
        self.assertEqual(2, len(quotes))
        self.assertEqual(('suspended', None, 'unknown'),
            (quotes[0]['status'], quotes[0]['price'], quotes[0]['score_basis']))
        self.assertEqual(('active', 'unknown'), (quotes[1]['status'], quotes[1]['score_basis']))
        self.assertEqual([], parse_quotes({'data': [row]}, 'leyu:another-fixture'))


class BasketballWinnerDetailTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_bound_single_match_client_preserves_winner(self):
        row = native_row()
        row['mid'] = '5833477'
        seen = []

        async def handler(request):
            self.assertEqual('POST', request.method)
            self.assertEqual(DEFAULT_MATCH_ODDS_PATH, request.url.path)
            body = json.loads(request.content)
            self.assertEqual('5833477', body['mid'])
            seen.append(body)
            return httpx.Response(200, json={'code': '0', 'data': {'data': [row]}})

        client = LeyuClient('https://upstream.invalid', request_id='isolated-session',
                            transport=httpx.MockTransport(handler))
        result = await client.fetch_match('5833477', fixture={
            'id': 'leyu:5833477', 'source_match_id': '5833477', 'phase': 'scheduled',
            'sport': 'basketball',
        })
        self.assertEqual(1, len(seen))
        self.assertTrue(result['markets_complete'])
        self.assertEqual(['home', 'away'], [q['selection'] for q in result['markets']])
        self.assertTrue(all(q['market_kind'] == 'winner' and q['market_id'] == '37'
                            and q['line'] is None for q in result['markets']))
        self.assertEqual('leyu:5833477', result['_current_observation']['fixture_id'])
