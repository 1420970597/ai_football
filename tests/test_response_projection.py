import copy
import threading
import time
import unittest
from unittest.mock import MagicMock

from api.app import ApiApp
from service.response_projection import ResponseProjection, compact_recommendations


class CompactRecommendationsTests(unittest.TestCase):
    def payload(self, count=1000):
        return {
            'open': [{
                'match_id': 'same-match', 'home': '主队', 'away': '客队',
                'score': [0, 1], 'clock': 25, 'market_open': True,
                'stale': False, 'recommendation_retained': True,
                'pick': {
                    'decision_id': f'open-{i}', 'market': 'OU', 'line': str(i / 2),
                    'outcome': 'over', 'label': f'大{i / 2}', 'odds': 1.95 + i / 100,
                    'odds_live': True, 'p_model': .71, 'confidence': 0,
                    'composite_confidence': .75, 'hit_count': 0, 'miss_count': 3.5,
                    'pending_count': 2, 'settled_hit_count': 1.5, 'settled_samples': 5,
                    'decision_count': 8, 'decision_occurrences': 8, 'pending_decisions': 2,
                    'historical_accuracy': .3, 'effective_ev_live': .04, 'kelly': .02,
                    'feature_context': {'private_feature': [1] * 100},
                    'weights': {'algorithm': .5}, 'members': [{'p_model': .71}],
                    'model_application': {'weights': [1] * 100},
                    'blend_weights': {'algorithm': .5}, 'decision_evidence': {'n': 30},
                    'training_history': [[1, 2, 3]], 'order_detail': {'native': 'metadata'},
                    'ts_ms': 20, 'quote_received_at': 1.0, 'quote_received_at_ms': 1000,
                    'input_cutoff_ms': 2000, 'quote_time_ms': 2000,
                    'evidence_at': '2026-10-14T12:00:00Z', 'probability_sum': 1,
                    'q_fill': .9, 'risk_penalty': .01, 'devig_spread_pp': .05,
                    'win_stake': .9, 'loss_stake': .1, 'push_stake': 0,
                    'tail': .01, 'disagreement': .02,
                },
                'betting': {'status': 'pending', 'reason': '等待场馆确认', 'attempt': 2,
                            'quote_to_submit_ms': 2200, 'order_no': 'display-example',
                            'retry_scheduled': True, 'retry_after_s': 30},
            } for i in range(3)],
            'closed': [{
                'at': f'2026-10-14T00:{i:04d}', 'match_id': f'match-{i // 3}',
                'decision_id': f'closed-{i}', 'algorithm': 'economic_ensemble',
                'competition_type': 'real', 'league': '联赛', 'home': '主队', 'away': '客队',
                'market': 'OU', 'line': '2.5', 'outcome': 'over', 'label': '大2.5',
                'market_label': '进球数', 'odds': 2.05, 'status': 'half_lost',
                'pnl': -.5, 'correct': False,
                'weights': {'algorithm': .5}, 'model_application': {'details': 'x' * 1000},
            } for i in range(count)],
            'summary': {'open': 3, 'closed': 2300, 'won': 1100, 'lost': 1000, 'pending': 200},
            'source_age_s': .25, 'generated_at': '2026-10-14T12:00:00Z',
            'projection': {'loading': False, 'stale': False, 'age_s': .1, 'error': ''},
        }

    def test_all_open_selections_values_and_execution_display_are_preserved(self):
        payload = self.payload()
        result = compact_recommendations(payload)
        self.assertEqual(len(result['open']), 3)
        diagnostics = {'feature_context', 'weights', 'members', 'model_application',
                       'blend_weights', 'decision_evidence', 'training_history', 'order_detail',
                       'ts_ms', 'quote_received_at', 'quote_received_at_ms', 'input_cutoff_ms',
                       'quote_time_ms', 'evidence_at', 'probability_sum', 'q_fill', 'risk_penalty',
                       'devig_spread_pp', 'win_stake', 'loss_stake', 'push_stake', 'tail',
                       'disagreement'}
        for before, after in zip(payload['open'], result['open']):
            self.assertEqual({k: v for k, v in before.items() if k != 'pick'},
                             {k: v for k, v in after.items() if k != 'pick'})
            self.assertEqual(after['pick'], {k: v for k, v in before['pick'].items()
                                            if k not in diagnostics})
        self.assertEqual(result['summary'], payload['summary'])
        self.assertEqual(result['projection'], payload['projection'])
        self.assertEqual(result['generated_at'], payload['generated_at'])

    def test_closed_pages_keep_identity_order_and_summary_window_scope(self):
        payload = self.payload()
        identities = []
        for offset in range(0, 1000, 100):
            result = compact_recommendations(payload, offset=offset)
            self.assertEqual(result['closed'], [
                {key: value for key, value in row.items()
                 if key not in {'weights', 'model_application'}}
                for row in payload['closed'][offset:offset + 100]])
            identities.extend(row['decision_id'] for row in result['closed'])
            self.assertEqual(result['closed_page'], {
                'offset': offset, 'limit': 100, 'returned': 100, 'total_available': 1000,
                'next_offset': offset + 100 if offset < 900 else None,
                'prev_offset': offset - 100 if offset else None,
                'summary_total': 2300, 'window_truncated': True,
                'scope': 'recent_recommendations',
            })
        self.assertEqual(identities, [row['decision_id'] for row in payload['closed']])

    def test_end_out_of_range_and_shrunk_history_clamp_to_last_page(self):
        payload = self.payload(151)
        end = compact_recommendations(payload, offset=150)
        self.assertEqual(end['closed_page']['offset'], 150)
        self.assertEqual(end['closed_page']['returned'], 1)
        self.assertIsNone(end['closed_page']['next_offset'])
        clamped = compact_recommendations(payload, offset=900)
        self.assertEqual(clamped['closed_page']['offset'], 100)
        self.assertEqual(clamped['closed_page']['returned'], 51)
        self.assertEqual(clamped['closed'][0]['decision_id'], 'closed-100')

    def test_empty_history_stays_empty_with_zero_offset(self):
        result = compact_recommendations({'open': [], 'closed': [], 'summary': {'closed': 0}},
                                         offset=900)
        self.assertEqual(result['closed_page'], {
            'offset': 0, 'limit': 100, 'returned': 0, 'total_available': 0,
            'next_offset': None, 'prev_offset': None, 'summary_total': 0,
            'window_truncated': False, 'scope': 'recent_recommendations',
        })

    def test_limit_one_and_untruncated_summary(self):
        payload = self.payload(2)
        payload['summary']['closed'] = 2
        result = compact_recommendations(payload, limit=1, offset=1)
        self.assertEqual(result['closed_page']['returned'], 1)
        self.assertFalse(result['closed_page']['window_truncated'])
        self.assertEqual(result['closed_page']['prev_offset'], 0)

    def test_invalid_summary_total_falls_back_to_visible_count_without_changing_summary(self):
        for value in (None, True, False, float('nan'), '2300', -1, 2 ** 53):
            with self.subTest(value=value):
                payload = self.payload(2)
                payload['summary']['closed'] = value
                result = compact_recommendations(payload)
                self.assertEqual(result['closed_page']['summary_total'], 2)
                self.assertFalse(result['closed_page']['window_truncated'])
                self.assertIs(result['summary']['closed'], value)

    def test_projection_does_not_change_the_cached_source(self):
        payload = self.payload()
        original = copy.deepcopy(payload)
        compact_recommendations(payload, offset=100)
        compact_recommendations(payload, offset=900)
        self.assertEqual(payload, original)

    def test_invalid_pure_projection_arguments_are_rejected(self):
        for limit, offset in ((0, 0), (101, 0), (1.5, 0), (True, 0),
                              (100, -1), (100, .5), (100, False)):
            with self.subTest(limit=limit, offset=offset), self.assertRaises(ValueError):
                compact_recommendations({}, limit=limit, offset=offset)


class ResponseProjectionTests(unittest.TestCase):
    def test_cold_read_and_published_age_do_not_build_in_request(self):
        build = MagicMock(return_value={'matches': [{'match_id': 'm'}]})
        cache = ResponseProjection(build)
        self.assertTrue(cache.read('real', 'workbench', {})['projection']['loading'])
        build.assert_not_called()
        cache.refresh()
        result = cache.read('real', 'workbench', {})
        self.assertEqual(result['matches'][0]['match_id'], 'm')
        self.assertFalse(result['projection']['loading'])
        self.assertIsNotNone(result['projection']['age_s'])
        build.assert_called_once()

    def test_failure_keeps_last_good_snapshot_with_explicit_error(self):
        build = MagicMock(return_value={'open': ['old']})
        cache = ResponseProjection(build)
        cache.read('real', 'recommendations', {})
        cache.refresh()
        build.side_effect = RuntimeError('secret provider response')
        cache.refresh()
        result = cache.read('real', 'recommendations', {})
        self.assertEqual(result['open'], ['old'])
        self.assertTrue(result['projection']['stale'])
        self.assertNotIn('secret', result['projection']['error'])

    def test_slow_background_does_not_block_multiple_browser_reads(self):
        started, release = threading.Event(), threading.Event()
        def build(kind, name):
            started.set()
            release.wait(2)
            return {'matches': []}
        cache = ResponseProjection(build, interval=.02)
        cache.read('real', 'workbench', {})
        cache.start()
        try:
            self.assertTrue(started.wait(1))
            before = time.monotonic()
            for _ in range(100):
                cache.read('real', 'workbench', {})
            self.assertLess(time.monotonic()-before, .1)
        finally:
            release.set()
            cache.stop()

    def test_api_cached_read_never_calls_analysis(self):
        analysis = MagicMock()
        app = ApiApp(MagicMock(), analysis=analysis)
        app._projection = ResponseProjection(MagicMock())
        self.assertTrue(app.h_workbench({}, {})['projection']['loading'])
        self.assertTrue(app.h_recommendations({}, {})['projection']['loading'])
        analysis.live_expert.results.assert_not_called()
        analysis.ledger.recommendation_entries.assert_not_called()
