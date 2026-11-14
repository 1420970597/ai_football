import unittest
import test_mcp_review_batch as support


class TeamTotalBatchTests(support.ReviewBatchTests):
    async def test_unknown_source_period_still_identifies_fixture_without_reading_or_recording(self):
        calls, _ = await self.install()
        result = await self.call([self.item(period=None, source_periods=[])])
        self.assertTrue(result['items'][0]['result']['resolution']['resolved'])
        self.assertEqual('source_period_unknown', result['items'][0]['result']['source_intent_recording']['reason'])
        self.assertEqual(['search_matches'], [path for path,_ in calls])

    async def test_source_team_total_records_only_verified_selected_side(self):
        def totals(path, body, result):
            if path == 'list_match_markets':
                result['markets'] = [dict(quote_id='team:'+side, market_kind='team_total',
                    period='full_time', team_side=side, selection='over', line='2.75',
                    selection_line='2.75', status='active', can_record=True) for side in ('home', 'away')]
                result['market_filter'] = dict(market_kind='team_total', period='full_time', line='2.75', selection='over')
                result['market_coverage'].update(matched_quotes=2, returned_quotes=2)
            return result
        calls, _ = await self.install(mutation=totals)
        result = await self.call([self.item(market_kind='team_total', market_query='大2.5/3',
            selection_team='team_b', selection_line=None, evidence_text='Away0进球大2.5/3')])
        record = next(body for path, body in calls if path == 'record_expert_signals')
        self.assertEqual('team:away', record['signals'][0]['quote_id'])
        self.assertEqual(1, len(result['items'][0]['result']['record_result']['accepted']))

    async def test_missing_native_side_or_multiple_same_side_never_records(self):
        for sides in ((None,), ('away', 'away')):
            def totals(path, body, result):
                if path == 'list_match_markets':
                    result['markets'] = [dict(quote_id=str(i), market_kind='team_total',
                        period='full_time', team_side=side, selection='over', line='2.75',
                        selection_line='2.75', status='active', can_record=True) for i,side in enumerate(sides)]
                    result['market_filter'] = dict(market_kind='team_total', period='full_time', line='2.75', selection='over')
                    result['market_coverage'].update(matched_quotes=len(sides), returned_quotes=len(sides))
                return result
            calls, _ = await self.install(mutation=totals)
            await self.call([self.item(market_kind='team_total', market_query='大2.5/3',
                selection_team='team_b', selection_line=None)])
            self.assertFalse(any(path == 'record_expert_signals' for path,_ in calls))

    async def test_missing_source_team_rejected_before_search(self):
        calls, _ = await self.install()
        response = await self.rpc('tools/call', {'name': 'review_match_batch', 'arguments': {
            'review_id': 'job', 'attempt_id': 'active', 'items': [self.item(
                market_kind='team_total', market_query='大2.5/3', selection_team=None, selection_line=None)]}})
        result = response.json()['result']
        self.assertTrue(result.get('isError'))
        self.assertEqual([], calls)
