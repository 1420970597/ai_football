import unittest
from decimal import Decimal

from app import review_state
from app.review_tool_evidence import encode_snapshot, replay_snapshot, snapshot_result


class ReviewContextDiagnosticsTests(unittest.TestCase):
    def context(self):
        quote = dict(market_kind='total', period='full_time', score_basis='whole_period',
                     play_id='p', market_instance_id='m', selection_id='s', fixture_id='f',
                     market_id='total-1', line=Decimal('1.75'), selection='over')
        fixture = dict(phase='live', home_score=1, away_score=0,
                       half_home_score=None, half_away_score=None)
        return review_state.quote_context(quote, fixture)

    def test_equal_context_has_no_changed_fields(self):
        current = self.context()
        self.assertEqual([], review_state.quote_context_changed_fields(dict(current), current))

    def test_every_fixed_context_field_reports_only_its_name(self):
        current = self.context()
        for key in current:
            with self.subTest(field=key):
                previous = dict(current, **{key: 'changed-private-value'})
                self.assertEqual([key], review_state.quote_context_changed_fields(previous, current))

    def test_missing_null_field_is_still_a_difference(self):
        current = self.context()
        previous = dict(current)
        del previous['half_home_score']
        self.assertEqual(['half_home_score'],
                         review_state.quote_context_changed_fields(previous, current))

    def test_confirmation_metadata_is_excluded_from_comparison(self):
        current = self.context()
        previous = dict(current, current_confirmation={'private': 'not-a-context-field'})
        self.assertEqual([], review_state.quote_context_changed_fields(previous, current))

    def test_unknown_read_fields_are_redacted_and_collapsed(self):
        current = self.context()
        previous = dict(current, secret_key='private', other_arbitrary_key=None)
        self.assertEqual(['unexpected_read_context'],
                         review_state.quote_context_changed_fields(previous, current))

    def test_multiple_changed_fields_have_stable_order(self):
        current = self.context()
        previous = dict(current, home_score=0, phase='scheduled', line='2.0', unknown='private')
        self.assertEqual(['home_score', 'line', 'phase', 'unexpected_read_context'],
                         review_state.quote_context_changed_fields(previous, current))

    def test_changed_fields_survive_tool_whitelist_and_exact_snapshot_replay(self):
        result = {'expert': 'isolated-expert', 'accepted': [], 'rejected': [{
            'index': 0, 'reason': 'quote_or_score_context_changed',
            'context_changed_fields': ['home_score', 'phase'], 'private_extra': 'must-not-leave-service'}]}
        projected = snapshot_result('mcp_record_expert_signals', result)
        self.assertEqual([{'index': 0, 'reason': 'quote_or_score_context_changed',
                           'context_changed_fields': ['home_score', 'phase']}], projected['rejected'])
        manifest, chunks = encode_snapshot(projected)
        manifest['call_id'] = 'isolated-call'
        records = [{'call_id': manifest['call_id'], 'snapshot_sha256': manifest['sha256'],
                    'chunk_index': index, 'data_base64': data} for index, data in enumerate(chunks)]
        self.assertEqual(projected, replay_snapshot(manifest, records))
