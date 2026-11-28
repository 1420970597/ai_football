import json
import unittest
from datetime import datetime, timedelta

from app.review_tool_evidence import encode_snapshot
from infra.profitability.review_market_replay import availability, summarize


def response(options):
    manifest, data = encode_snapshot({
        'markets': options,
        'market_coverage': {'complete_for_filter': True, 'returned_quotes': len(options)},
    })
    manifest.update(call_id='test-call', references={'quote_ids': sorted(q['quote_id'] for q in options)})
    chunks = [
        {'chunk_index': index, 'call_id': manifest['call_id'], 'snapshot_sha256': manifest['sha256'],
         'data_base64': content}
        for index, content in enumerate(data)
    ]
    return {'call_id': manifest['call_id'], 'response_snapshot': manifest}, chunks


class ReviewMarketReplayTests(unittest.TestCase):
    def test_decodes_flags_and_distinguishes_usable_from_closed_and_unconfirmed(self):
        options = [
            {'quote_id': 'a', 'status': 'closed', 'can_record': False},
            {'quote_id': 'b', 'status': 'active', 'can_record': False},
        ]
        self.assertEqual(
            ('no_recordable_option', {'active_without_usable_freshness': 1, 'non_active': 1}),
            availability(*response(options)),
        )
        options[1]['can_record'] = True
        self.assertEqual('has_recordable_option', availability(*response(options))[0])
        del options[1]['can_record']
        self.assertEqual('option_availability_unknown', availability(*response(options))[0])

    def test_missing_chunks_and_mismatched_references_are_evidence_gaps(self):
        payload, chunks = response([{'quote_id': 'a', 'can_record': True}])
        self.assertEqual('snapshot_unavailable', availability(payload, [])[0])
        self.assertEqual('snapshot_unavailable',
                         availability(payload, [dict(chunks[0], call_id='other')])[0])
        payload['response_snapshot']['references']['quote_ids'] = ['other-quote']
        self.assertEqual('snapshot_unavailable', availability(payload, chunks)[0])

    def test_a_later_recordable_quote_does_not_count_within_the_source_window(self):
        posted = datetime.fromisoformat('2026-11-18T12:00:00Z')
        rows = []
        for job_id, offset, flag in (('initial', 300, False), ('initial', 301, True),
                                     ('second', 60, False), ('second', 120, True),
                                     ('third', 300, False), ('third', 301, None),
                                     ('fourth', -2, True)):
            payload, chunks = response([{'quote_id': 'a', 'status': 'closed', 'can_record': flag}])
            rows.append({
                'id': job_id, 'has_media': True, 'message_posted_at': posted,
                'read_at': posted + timedelta(seconds=offset), 'payload': json.dumps(payload),
                'chunks': json.dumps(chunks),
            })
        self.assertEqual(
            {'timely_no_recordable_option': 2, 'timely_recordable_option': 1, 'timing_unavailable': 1},
            {row['outcome']: row['count'] for row in summarize(rows)['jobs']},
        )
