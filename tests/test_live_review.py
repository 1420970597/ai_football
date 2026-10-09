import time
import unittest
from dataclasses import replace
from unittest.mock import MagicMock

from service.live_review import LiveReview
from service.runtime_settings import RuntimeConfig


class LiveReviewTests(unittest.TestCase):
    def row(self):
        return {'match_id': 'm', 'config_version': 2, 'score': [1, 0], 'computed_at': '2026-10-09T12:00:00Z',
                'published_at_ms': int(time.time() * 1000), 'forecasts': [{'label': '主胜'}]}

    def review(self):
        review = LiveReview()
        review.configure(replace(RuntimeConfig(), llm_enabled=True, llm_base_url='http://localhost:9999/v1', llm_model='test-model'), 2)
        review._client = MagicMock()
        review._client.complete.return_value='{"verdict":"watch","confidence":0.5,"reason":"证据不足"}'
        return review

    def test_disabled_no_network_and_enabled_async_output(self):
        row = self.row()
        off = LiveReview()
        off.submit(row)
        self.assertFalse(off.process_one())
        self.assertEqual(off.public(row)['status'], 'disabled')
        review = self.review()
        review.submit(row)
        review._client.complete.assert_not_called()
        self.assertTrue(review.process_one())
        review._client.complete.assert_called_once()
        self.assertEqual(review.public(row)['verdict'], 'watch')
        self.assertEqual(review.public({**row, 'score': [2, 0]})['status'], 'expired')
        review.submit(row)
        self.assertFalse(review.process_one())  # per-match interval is honored

    def test_failures_and_old_config_responses_are_never_used(self):
        row = self.row()
        review = self.review()
        review._client.complete.return_value='{"verdict":"watch","confidence":2}'
        review.submit(row)
        review.process_one()
        self.assertEqual(review.public(row)['status'], 'error')
        review = self.review()
        def changed(*_, **__):
            review.configure(RuntimeConfig(), 3)
            return '{"verdict":"confirm","confidence":1}'
        review._client.complete.side_effect=changed
        review.submit(row)
        review.process_one()
        self.assertEqual(review.discarded, 1)
        self.assertEqual(review._reviews, {})

    def test_expired_queued_snapshot_and_bounded_queue(self):
        review = self.review()
        row = self.row()
        for i in range(100):
            review.submit({**row, 'match_id': str(i)})
        self.assertEqual(review.health()['pending'], 64)
        self.assertEqual(review.discarded, 36)
        expired = self.review()
        expired.submit({**row, 'published_at_ms': 1})
        expired.process_one()
        expired._client.complete.assert_not_called()
        self.assertEqual(expired.discarded, 1)
