from datetime import datetime, timezone
from decimal import Decimal
from http.client import RemoteDisconnected
import json
import unittest
from unittest.mock import call, patch

from reference_orderbooks import (
    CLOB, GAMMA, capture, capture_market, kalshi_book, polymarket_book, polymarket_us_book,
    polymarket_us_sides, replay_kalshi_tickers,
)


class ReferenceOrderBookTests(unittest.TestCase):
    def test_polymarket_order_book_checks_identity_and_preserves_native_time(self):
        book = {
            'market': 'condition', 'asset_id': 'token', 'timestamp': '1795044465721',
            'bids': [{'price': '0.60', 'size': '475.12'}, {'price': '0.58', 'size': '1200'}],
            'asks': [{'price': '0.64', 'size': '1222'}, {'price': '0.61', 'size': '3400.75'}],
        }
        received = datetime(2026, 11, 18, 23, 27, 46, tzinfo=timezone.utc)
        result = polymarket_book(book, 'condition', 'token', received)
        self.assertEqual(('two_sided', Decimal('0.60'), Decimal('0.61')),
                         (result['status'], result['bid'], result['ask']))
        self.assertEqual(Decimal('475.12'), result['bid_size'])
        self.assertEqual(0.279, result['quote_age_seconds'])
        with self.assertRaises(ValueError):
            polymarket_book(book, 'condition', 'other-token', received)

    def test_kalshi_uses_no_bid_for_yes_ask_without_fabricating_time(self):
        def book(yes, no):
            return kalshi_book({'orderbook_fp': {'yes_dollars': yes, 'no_dollars': no}})

        result = book([('0.59', '6')], [('0.40', '8')])
        self.assertEqual({'status': 'two_sided', 'bid': Decimal('0.59'), 'ask': Decimal('0.60'),
                          'bid_size': Decimal(6), 'ask_size': Decimal(8), 'quote_time': None}, result)
        self.assertEqual('crossed_or_locked', book([('0.61', '6')], [('0.40', '8')])['status'])
        self.assertEqual('missing_side', book([], [('0.40', '8')])['status'])
        with self.assertRaises(ValueError):
            book([('0.59', '0')], [('0.40', '8')])

    def test_polymarket_us_capture_checks_market_and_preserves_order_book_time(self):
        slug = 'atc-fixture-home'
        received = datetime(2026, 11, 18, 23, 27, 46, tzinfo=timezone.utc)
        book_time = '2026-11-18T23:27:45.721064983Z'
        market = {'slug': slug, 'id': '123', 'outcomes': '["Yes","No"]', 'active': True, 'closed': False,
                  'description': 'Regulation play', 'sportsMarketType': 'soccer_team_full_time_winner',
                  'marketSides': [{'description': 'Yes', 'marketId': 123, 'long': True},
                                  {'description': 'No', 'marketId': 123, 'long': False}]}
        book = {
            'marketSlug': slug, 'transactTime': book_time, 'state': 'MARKET_STATE_OPEN',
            'bids': [{'px': {'value': '0.58', 'currency': 'USD'}, 'qty': '7.25'}],
            'offers': [{'px': {'value': '0.59', 'currency': 'USD'}, 'qty': '3.125'}],
        }
        with patch('reference_orderbooks.get_json', side_effect=[{'market': market}, {'marketData': book}]), \
                patch('reference_orderbooks.now', return_value=received):
            observation = capture_market('polymarket_us', slug)
        quote, = observation['books']
        self.assertEqual('123', observation['market_id'])
        self.assertEqual('Yes', quote['outcome'])
        self.assertEqual('two_sided', quote['status'])
        self.assertEqual(book_time, quote['native_transact_time'])
        self.assertEqual(0.278936, quote['quote_age_seconds'])
        self.assertEqual(Decimal('3.125'), quote['ask_size'])
        for change in ({'marketSlug': 'different'}, {'transactTime': None},
                       {'transactTime': '2026-11-18T23:28:00Z'},
                       {'bids': [{'px': {'value': '0.58', 'currency': 'EUR'}, 'qty': '7'}]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                polymarket_us_book(book | change, slug, received)
        suspended = polymarket_us_book(book | {'state': 'MARKET_STATE_SUSPENDED'}, slug, received)
        self.assertEqual('market_not_open', suspended['status'])
        self.assertNotIn('bid', suspended)
        self.assertEqual('missing_side', polymarket_us_book(book | {'bids': []}, slug, received)['status'])

    def test_polymarket_us_uses_native_direction_for_named_outcomes(self):
        home = {'description': 'Nuggets', 'marketId': 123, 'long': False}
        away = {'description': 'Celtics', 'marketId': 123, 'long': True}
        market = {'id': '123', 'outcomes': '["Nuggets","Celtics"]', 'marketSides': [home, away]}
        self.assertEqual({True: 'Celtics', False: 'Nuggets'}, polymarket_us_sides(market))
        for change in ({'long': False}, {'description': 'Other'}, {'marketId': 456}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                polymarket_us_sides(market | {'marketSides': [home, away | change]})

    def test_failed_market_does_not_interrupt_the_sample(self):
        observation = {'source': 'kalshi', 'market': 'SECOND'}
        with patch('reference_orderbooks.get_json', side_effect=RemoteDisconnected()), \
                patch('reference_orderbooks.capture_market', return_value=observation):
            sample = capture([('polymarket', 'first'), ('kalshi', 'SECOND')])
        self.assertEqual('RemoteDisconnected', sample['markets'][0]['error'])
        self.assertEqual(observation, sample['markets'][1])

    def test_polymarket_batch_matches_books_by_asset_and_condition(self):
        def market(identifier):
            return {'slug': identifier, 'conditionId': identifier, 'description': 'Match rules',
                    'outcomes': '["Yes","No"]', 'acceptingOrders': True,
                    'clobTokenIds': json.dumps([f'{identifier}-yes', f'{identifier}-no'])}

        def book(identifier, side, timestamp='1795044465721'):
            return {'market': identifier, 'asset_id': f'{identifier}-{side}', 'timestamp': timestamp,
                    'bids': [{'price': '0.58', 'size': '6'}], 'asks': [{'price': '0.64', 'size': '8'}]}

        markets = [('polymarket', 'first'), ('polymarket', 'second')]
        first_yes, first_no = book('first', 'yes'), book('first', 'no', timestamp='1795044465121')
        second_yes, second_no = book('second', 'yes'), book('second', 'no')
        received = datetime(2026, 11, 18, 23, 27, 46, tzinfo=timezone.utc)
        with patch('reference_orderbooks.get_json', side_effect=[
            market('first'), market('second'), [second_no, first_no, second_yes, first_yes],
        ]) as request, patch('reference_orderbooks.now', return_value=received):
            first, second = capture(iter(markets))['markets']
        request.assert_has_calls([
            call(f'{GAMMA}/markets/slug/first'), call(f'{GAMMA}/markets/slug/second'),
            call(f'{CLOB}/books', body=[{'token_id': token} for token in (
                'first-yes', 'first-no', 'second-yes', 'second-no',
            )]),
        ])
        self.assertEqual(3, request.call_count)
        self.assertEqual(['first', 'second'], [first['condition_id'], second['condition_id']])
        self.assertEqual(['Yes', 'No'], [side['outcome'] for side in first['books']])
        self.assertEqual([0.279, 0.879], [side['quote_age_seconds'] for side in first['books']])
        self.assertTrue(all(side['received_at'] == received for row in (first, second) for side in row['books']))

        for invalid_first in ([first_yes], [first_yes, first_no, first_yes],
                              [first_no, first_yes | {'market': 'wrong'}]):
            with self.subTest(invalid_first=invalid_first), patch('reference_orderbooks.get_json', side_effect=[
                market('first'), market('second'), [*invalid_first, second_yes, second_no],
            ]):
                first, second = capture(markets)['markets']
            self.assertIn('error', first)
            self.assertEqual(2, len(second['books']))

        kalshi = {'source': 'kalshi', 'market': 'TICKER'}
        with patch('reference_orderbooks.get_json', side_effect=[market('first'), RemoteDisconnected()]), \
                patch('reference_orderbooks.capture_market', return_value=kalshi):
            first, second = capture([markets[0], ('kalshi', 'TICKER')])['markets']
        self.assertEqual('RemoteDisconnected', first['error'])
        self.assertEqual(kalshi, second)

    def test_ticker_replay_uses_original_receive_time_and_native_update_time(self):
        frame = {'type': 'ticker', 'sending_ts_ms': 1795044465722, 'msg': {
            'market_ticker': 'TARGET', 'ts_ms': 1795044465721,
            'yes_bid_dollars': '0.59', 'yes_ask_dollars': '0.60',
            'yes_bid_size_fp': '5.25', 'yes_ask_size_fp': '8.10',
        }}
        row = {'frame': frame, 'received_at': '2026-11-18T23:27:46Z'}
        result, = replay_kalshi_tickers([json.dumps(row)], {'TARGET'})
        self.assertEqual(0.279, result['quote_age_seconds'])
        self.assertEqual(Decimal('5.25'), result['bid_size'])
        self.assertEqual(1795044465722, result['sending_ts_ms'])
        for change, tickers in (({'ts_ms': None}, {'TARGET'}),
                                ({'ts_ms': 1795044466001}, {'TARGET'}),
                                ({'yes_ask_size_fp': '0'}, {'TARGET'}),
                                ({}, {'OTHER'})):
            with self.subTest(change=change, tickers=tickers):
                broken = row | {'frame': frame | {'msg': frame['msg'] | change}}
                results = list(replay_kalshi_tickers([json.dumps(broken), json.dumps(row)], tickers))
                self.assertEqual('invalid_ticker_observation', results[0]['error'])
                if tickers == {'TARGET'}:
                    self.assertEqual(result, results[1])

    def test_event_catalog_batches_only_explicit_unambiguous_markets(self):
        identifiers = [f'line-{number}' for number in range(11)]
        markets = [{'slug': slug, 'conditionId': slug, 'description': 'Match rules',
                    'outcomes': '["Over","Under"]', 'acceptingOrders': True,
                    'clobTokenIds': json.dumps([f'{slug}:over', f'{slug}:under'])}
                   for slug in identifiers]
        # Ten listed markets, including one duplicate, and one absent requested identifier.
        catalog = {'slug': 'match-market-catalog', 'markets': [*markets[:10], markets[9]]}
        requested = [('polymarket', slug) for slug in identifiers]

        def request(url, *, body=None):
            if url == f'{GAMMA}/events/slug/match-market-catalog':
                return catalog
            self.assertEqual(f'{CLOB}/books', url)
            return [{'market': item['token_id'].split(':')[0], 'asset_id': item['token_id'],
                     'timestamp': '1795044465721', 'bids': [{'price': '0.45', 'size': '4'}],
                     'asks': [{'price': '0.55', 'size': '5'}]}
                    for item in reversed(body)]

        with patch('reference_orderbooks.get_json', side_effect=request) as query:
            observations = capture(requested, 'match-market-catalog')['markets']
        self.assertEqual(3, query.call_count)
        self.assertEqual([16, 2], [len(call.kwargs['body']) for call in query.call_args_list[1:]])
        self.assertTrue(all(len(row['books']) == 2 for row in observations[:9]))
        self.assertEqual(['ValueError', 'ValueError'], [row['error'] for row in observations[9:]])
