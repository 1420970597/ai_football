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

    def test_suffix_totals_have_the_same_explicit_scope_as_prefix(self):
        for suffix,prefix in (('1.5大','大1.5'),('0.5/1大','大0.5/1'),('3/2.5小','小3/2.5')):
            with self.subTest(query=suffix):
                self.assertEqual(self.filters(prefix),self.filters(suffix))

    def test_suffix_queries_never_supply_a_period_or_override_scope(self):
        result=self.filters('1.5大')
        self.assertIsNone(result['period'])
        self.assertEqual(Decimal('1.5'),result['line'])
        for overrides in ({'market_kind':'handicap'},{'selection':'under'},{'line':Decimal('2.5')}):
            with self.subTest(overrides=overrides):
                original=dict(market_kind=None,period=None,line=None,selection=None,market_query='1.5大')|overrides
                self.assertEqual(original,market_filters(SimpleNamespace(**original)))

    def test_suffix_query_requires_a_complete_valid_expression(self):
        for query in ('1.5大赔率1.9','比分1-0大','@1.5大','大1.5小','大小1.5','0.5/1.5大','1.3大','100000大'):
            with self.subTest(query=query):
                self.assertIsNone(self.filters(query)['line'])
                self.assertEqual(query,self.filters(query)['market_query'])

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
