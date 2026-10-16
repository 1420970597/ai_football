import copy
import threading
import time
import unittest
from unittest.mock import MagicMock

from api.app import ApiApp
from service.response_projection import ResponseProjection


class ResponseProjectionTests(unittest.TestCase):
    def test_compact_recommendations_preserve_displayed_rows_and_full_response(self):
        rows = [{'at': '2026-10-16T00:00:00Z', 'match_id': str(index),
                 'home': '主队', 'away': '客队', 'label': '全场大2.5', 'market': 'OU',
                 'odds': 1.9, 'status': 'won', 'pnl': .9,
                 'weights': {'model': .5}, 'model_application': {'audit': 'details'}}
                for index in range(1000)]
        payload = {'closed': rows, 'open': [{'pick': {'confidence': .6}, 'betting': {'status': 'disabled'}}],
                   'summary': {'closed': 3000, 'won': 2000, 'lost': 1000},
                   'source_age_s': 1.5, 'generated_at': '2026-10-16T00:00:01Z'}
        original = copy.deepcopy(payload)
        for projected in (False, True):
            with self.subTest(projected=projected):
                app = ApiApp(MagicMock())
                app._build_recommendations = MagicMock(return_value=payload)
                if projected:
                    app._projection = ResponseProjection(MagicMock(return_value=payload))
                    app._projection.read('real', 'recommendations', {})
                    app._projection.refresh()
                full = app.h_recommendations({}, {})
                compact = app.h_recommendations({'view': ['compact']}, {})
                self.assertEqual(len(compact['closed']), 1000)
                self.assertFalse(compact['closed'] is payload['closed'])
                for index, row in enumerate(compact['closed']):
                    self.assertEqual(row, {k: v for k, v in rows[index].items()
                                           if k not in ('weights', 'model_application')})
                    self.assertIsNot(row, rows[index])
                for key in ('open', 'summary', 'source_age_s', 'generated_at'):
                    self.assertEqual(compact[key], payload[key])
                if projected:
                    self.assertFalse(compact['projection']['loading'])
                    self.assertEqual(compact['projection']['error'], '')
                self.assertEqual(full['closed'], rows)
                self.assertEqual(app.h_recommendations({'view': ['full']}, {})['closed'], rows)
                self.assertEqual(payload, original)
                compact['closed'][0]['home'] = 'changed'
                self.assertEqual(payload, original)

    def test_compact_recommendations_handle_cold_empty_and_invalid_views(self):
        app = ApiApp(MagicMock())
        app._projection = ResponseProjection(MagicMock(return_value={'open': [], 'summary': {}}))
        cold = app.h_recommendations({'view': ['compact']}, {})
        self.assertEqual(cold['closed'], [])
        self.assertTrue(cold['projection']['loading'])
        app._projection.refresh()
        self.assertEqual(app.h_recommendations({'view': ['compact']}, {})['closed'], [])
        for projection in (app._projection, None):
            app._projection = projection
            code, _ = app.dispatch('GET', '/api/v1/recommendations', {'view': ['invalid']}, {})
            self.assertEqual(code, 400)

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
