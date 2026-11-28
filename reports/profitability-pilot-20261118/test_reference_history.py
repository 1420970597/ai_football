from datetime import datetime, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch

import reference_history


class ReferenceHistoryTests(unittest.TestCase):
    cutoff = datetime(2026, 1, 1, 12, 0, 55, 300000, tzinfo=timezone.utc)

    def test_lookback_rejects_old_and_future_data(self):
        second = int(self.cutoff.timestamp())
        old = {'t': second - 600}
        future = {'t': second + 1}
        latest = {'t': second}
        self.assertIsNone(reference_history.at_or_before([old, future], 't', self.cutoff))
        self.assertEqual(reference_history.at_or_before([future, latest, old], 't', self.cutoff), latest)

    def test_token_history_keeps_each_outcome_own_point(self):
        second = int(self.cutoff.timestamp())
        responses = [
            {'outcomes': '["Over","Under"]', 'clobTokenIds': '["1","2"]', 'question': 'Total 2.5'},
            {'history': [{'t': second - 30, 'p': Decimal('0.55')}, {'t': second + 1, 'p': Decimal('0.64')}]},
            {'history': []},
        ]
        with patch.object(reference_history, 'get_json', side_effect=responses) as api:
            report = reference_history.polymarket_history('soccer-total', self.cutoff)
        self.assertEqual(report['observation_kind'], 'token_price_history')
        self.assertEqual(report['outcomes'][0]['point']['age_seconds'], 30.3)
        self.assertEqual(report['outcomes'][0]['point']['price'], Decimal('0.55'))
        self.assertIsNone(report['outcomes'][1]['point'])
        self.assertEqual(api.call_args_list[1].kwargs['endTs'], second)

    def test_candles_use_closed_intervals(self):
        second = int(self.cutoff.timestamp())
        candle = {'end_period_ts': second - 55, 'yes_bid': {'close_dollars': '0.59'},
                  'yes_ask': {'close_dollars': '0.60'}, 'price': {'close_dollars': None}}
        responses = [{'market': {'title': 'Total 2.5'}},
                     {'candlesticks': [candle, {'end_period_ts': second + 5}]}]
        with patch.object(reference_history, 'get_json', side_effect=responses):
            report = reference_history.kalshi_history('SOCCER', 'SOCCER-GAME', self.cutoff)
        self.assertEqual(report['observation_kind'], 'closed_one_minute_candle')
        self.assertEqual(report['candle']['age_seconds'], 55.3)
        self.assertEqual(report['candle']['yes_ask'], '0.60')
        self.assertIsNone(report['candle']['price'])

    def test_shared_candle_requires_the_same_closed_minute_and_valid_bid_ask(self):
        minute = int(self.cutoff.timestamp()) // 60 * 60

        def candle(stamp, bid='0.31', ask='0.33'):
            return {'end_period_ts': stamp, 'yes_bid': {'close_dollars': bid}, 'yes_ask': {'close_dollars': ask}}

        rows = {
            'A': [candle(minute), candle(minute - 60), candle(minute + 60)],
            'B': [candle(minute, ask=None), candle(minute - 60), candle(minute + 60)],
            'C': [candle(minute, bid='0.45'), candle(minute - 60), candle(minute + 60)],
        }
        report = reference_history.shared_kalshi_candle(rows, self.cutoff)
        self.assertEqual(report['candle']['age_seconds'], 115.3)
        self.assertEqual(set(report['candle']['quotes']), {'A', 'B', 'C'})
        rows['B'].append(candle(minute - 60))
        self.assertIsNone(reference_history.shared_kalshi_candle(rows, self.cutoff)['candle'])
