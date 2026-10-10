import threading
import time
import unittest
from unittest.mock import MagicMock

from api.app import ApiApp
from service.response_projection import ResponseProjection


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
