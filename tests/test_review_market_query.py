from decimal import Decimal
from types import SimpleNamespace
import unittest

from app.review_market_query import market_filters


class MarketQueryTests(unittest.TestCase):
    def filters(self, query, **overrides):
        return market_filters(SimpleNamespace(market_kind=None, period=None, line=None,
            selection=None, **({'market_query': query} | overrides)))

    def test_explicit_totals_keep_period_unspecified(self):
        self.assertEqual({'market_kind': 'total', 'period': None, 'line': Decimal('2.5'),
                          'selection': 'over', 'market_query': None}, self.filters('大2.5'))
        self.assertEqual('under', self.filters('小 2.5')['selection'])

    def test_split_totals_use_numeric_average(self):
        self.assertEqual(Decimal('.75'), self.filters('大0.5/1')['line'])
        self.assertEqual(Decimal('2.75'), self.filters('小3/2.5')['line'])

    def test_non_exact_or_invalid_expression_is_unchanged(self):
        for query in ('全场大小', '大2.5赔率1.9', '大2.5和小3', '角球大2.5',
                      '大-1', '大2.1', '小0/3', '大1/1', '大2.555', '大100000', '大', None):
            with self.subTest(query=query):
                result = self.filters(query)
                self.assertEqual(query, result['market_query'])
                self.assertIsNone(result['market_kind'])
                self.assertIsNone(result['line'])

    def test_conflicting_explicit_filters_are_not_overridden(self):
        for overrides in ({'market_kind': 'handicap'}, {'selection': 'under'},
                          {'line': Decimal('3')}, {'market_kind': 'unsupported'}):
            with self.subTest(overrides=overrides):
                source = dict(market_kind=None, period=None, line=None, selection=None, market_query='大2.5')
                source.update(overrides)
                self.assertEqual(source, market_filters(SimpleNamespace(**source)))

    def test_compatible_explicit_scope_is_preserved(self):
        body = SimpleNamespace(market_kind='total', period='first_half', line=Decimal('2.5'),
                               selection='over', market_query='大2.5')
        self.assertEqual('first_half', market_filters(body)['period'])
        self.assertEqual('大2.5', body.market_query)
