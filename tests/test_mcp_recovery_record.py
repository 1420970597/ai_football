"""A recovered exact total must not wait for another model turn to record."""
import asyncio
import json
import unittest

from test_mcp_single_round import SingleRoundRecordTests as Support


class RecoveryRecordTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = Support.asyncSetUp
    asyncTearDown = Support.asyncTearDown
    rpc = Support.rpc
    install = Support.install
    quote = Support.quote

    def arguments(self, **changes):
        return dict(review_id='job', attempt_id='current', fixture_id='leyu:123456',
            market_kind='total', period='first_half', market_query='1.5大',
            source_periods=['first_half'], record_source_intent=True,
            evidence_text='来源明确上半场1.5大') | changes

    async def call(self, **changes):
        response = await self.rpc('tools/call', {'name': 'list_match_markets',
            'arguments': self.arguments(**changes)})
        self.assertEqual(200, response.status_code)
        return response.json()['result']

    async def test_recovered_read_records_in_same_call_using_native_quote(self):
        calls = await self.install()
        result = await self.call()
        self.assertFalse(result.get('isError'), result)
        value = json.loads(result['content'][0]['text'])
        self.assertEqual(['list_match_markets', 'record_expert_signals'], [p for p, _ in calls])
        self.assertTrue(value['source_intent_recording']['attempted'])
        self.assertEqual('1.95000', value['record_result']['accepted'][0]['price'])
        self.assertEqual(('job', 'current'), (calls[1][1]['review_id'], calls[1][1]['attempt_id']))
        self.assertEqual(self.quote()['quote_id'], calls[1][1]['signals'][0]['quote_id'])
        self.assertEqual(self.arguments()['evidence_text'], calls[1][1]['signals'][0]['evidence_text'])
        self.assertNotIn('record_source_intent', calls[0][1])
        self.assertNotIn('evidence_text', calls[0][1])

    async def test_throttled_then_recovered_read_never_records_unready_quote(self):
        calls = await self.install(quotes=[self.quote(can_record=False)])
        result = await self.call()
        self.assertFalse(json.loads(result['content'][0]['text'])['source_intent_recording']['attempted'])
        self.assertEqual(['list_match_markets'], [p for p, _ in calls])
        calls = await self.install()
        result = await self.call()
        self.assertEqual(['list_match_markets', 'record_expert_signals'], [p for p, _ in calls])

    async def test_unknown_or_conflicting_source_scope_fails_before_native_read(self):
        for changes in ({'source_periods': []}, {'source_periods': None},
                        {'source_periods': ['full_time']}, {'period': 'first_quarter'},
                        {'evidence_text': ''}, {'market_query': None}, {'market_kind': 'handicap'}):
            with self.subTest(changes=changes):
                calls = await self.install()
                self.assertTrue((await self.call(**changes)).get('isError'))
                self.assertEqual([], calls)

    async def test_ineligible_incomplete_ambiguous_or_wrong_fixture_never_records(self):
        cases = [dict(quotes=[]), dict(quotes=[self.quote(), self.quote(quote_id='other')]),
            dict(quotes=[self.quote(status='suspended')]), dict(quotes=[self.quote(can_record=False)]),
            dict(quotes=[self.quote(period='full_time')]), dict(quotes=[self.quote(selection='under')]),
            dict(quotes=[self.quote(line='1.75')]), dict(market_fixture={'id': 'other'}),
            dict(coverage=dict(total_quotes=1, matched_quotes=1, returned_quotes=1, complete_for_filter=False))]
        for case in cases:
            with self.subTest(case=case):
                calls = await self.install(**case)
                self.assertFalse((await self.call()).get('isError'))
                self.assertEqual(['list_match_markets'], [p for p, _ in calls])

    async def test_legacy_read_has_no_new_side_effect_or_wrapped_response(self):
        calls = await self.install()
        result = await self.call(record_source_intent=False)
        value = json.loads(result['content'][0]['text'])
        self.assertIn('markets', value)
        self.assertNotIn('record_result', value)
        self.assertEqual(['list_match_markets'], [p for p, _ in calls])

    async def test_failed_native_read_does_not_record(self):
        calls = await self.install(failure='list_match_markets')
        self.assertTrue((await self.call()).get('isError'))
        self.assertEqual(['list_match_markets'], [p for p, _ in calls])

    async def test_unknown_record_receipt_preserves_read_without_retry(self):
        calls = await self.install(failure='record_expert_signals')
        result = await self.call()
        self.assertTrue(result.get('isError'))
        self.assertIn('completed_receipt', result['content'][0]['text'])
        self.assertEqual(['list_match_markets', 'record_expert_signals'], [p for p, _ in calls])

    async def test_cancellation_does_not_retry_or_leave_market_slot(self):
        entered = asyncio.Event()
        calls = await self.install(record_wait=entered)
        task = asyncio.create_task(self.server.list_match_markets(**self.arguments()))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(['list_match_markets', 'record_expert_signals'], [p for p, _ in calls])
        self.assertEqual(0, self.server.pending_market_calls)
        self.assertEqual(0, self.server.pending_market_requests)
