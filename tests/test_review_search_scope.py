import unittest
from app.review_search_scope import continuation_scope, scope_key


class ContinuationScopeTests(unittest.TestCase):
    def setUp(self):
        self.scope = dict(team='alpha', phase='all', competition='league',
                          sport='football', **{'from': '2026-11-01T00:00:00+00:00',
                                             'to': '2026-11-04T00:00:00+00:00'})
        self.key = scope_key(self.scope)

    def test_omitted_optional_filters_inherit_exact_scope(self):
        requested = {**self.scope, 'competition': '', 'sport': None, 'from': None, 'to': None}
        self.assertEqual(self.scope, continuation_scope(requested, self.scope, self.key))

    def test_each_explicit_conflict_is_rejected(self):
        for field in self.scope:
            with self.subTest(field=field), self.assertRaises(ValueError):
                continuation_scope({**self.scope, field: 'different'}, self.scope, self.key)

    def test_missing_corrupt_and_wrong_query_evidence_rejected(self):
        for evidence in (None, '', 'invalid json', {}, {**self.scope, 'sport': 'basketball'}):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                continuation_scope(self.scope, evidence, self.key)
