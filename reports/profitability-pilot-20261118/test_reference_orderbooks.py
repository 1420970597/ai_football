from datetime import datetime, timezone
from decimal import Decimal
from http.client import RemoteDisconnected
import unittest
from unittest.mock import patch

from reference_orderbooks import capture, kalshi_book, polymarket_book


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

    def test_failed_market_does_not_interrupt_the_sample(self):
        observation = {'source': 'kalshi', 'market': 'SECOND'}
        with patch('reference_orderbooks.capture_market', side_effect=[RemoteDisconnected(), observation]):
            sample = capture([('polymarket', 'first'), ('kalshi', 'SECOND')])
        self.assertEqual('RemoteDisconnected', sample['markets'][0]['error'])
        self.assertEqual(observation, sample['markets'][1])
