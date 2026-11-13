"""Bounded batch presentation; full native evidence remains in the API.

These are isolated MCP protocol fixtures, with no database or model calls.
"""
import asyncio
import copy
import json
import unittest
from unittest.mock import patch

from tests import test_mcp_transport as support


def restore_rows(value):
    if isinstance(value, list):
        return value
    assert value['encoding'] == 'shared-column-rows-v1'
    return [dict(zip(value['columns'], row)) | value['shared'] for row in value['rows']]


class BatchBudgetTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.MCPTransportTests.asyncSetUp
    asyncTearDown = support.MCPTransportTests.asyncTearDown
    rpc = support.MCPTransportTests.rpc

    def item(self, index=0, **updates):
        return self.server.SourceMatchIntent.model_validate({
            'team_a': f'Alpha{index}', 'team_b': f'Beta{index}', 'sport': 'football',
            'from_utc': '2026-11-12T00:00:00Z', 'to_utc': '2026-11-13T00:00:00Z',
            'market_kind': 'handicap', 'period': 'full_time', 'source_periods': ['full_time'],
            'selection_team': 'team_a', 'selection_line': '-.25',
            'evidence_text': f'全场Alpha{index}让0.25', **updates})

    def native(self, index=0, count=120, **updates):
        fixture = {'id': f'leyu:{1000+index}', 'sport': 'football',
            'home_team': f'Beta{index}', 'away_team': f'Alpha{index}',
            'phase': 'live', 'realtime_eligible': True}
        rows = []
        for offset in range(count):
            status = ('active', 'suspended', 'closed')[offset % 3]
            rows.append({'quote_id': f'{index:08d}-0000-4000-8000-{offset:012d}',
                'market_name': '全场亚洲让球附加盘口', 'period': 'full_time',
                'market_kind': 'handicap', 'line': '0.25', 'selection': 'away',
                'status': status, 'score_basis': 'remaining', 'fresh': True,
                'confirmed_current': status == 'active', 'can_record': status == 'active',
                'team_side': None, 'line_basis': 'provider_home', 'selection_line': '-0.25'})
        return {'search_key': f'{index:064x}', 'search_complete': True, 'next_cursor': None,
            'total_candidates': 1, 'items': [fixture],
            'resolution': {'resolved': True, 'source_team_sides': {'team_a': 'away', 'team_b': 'home'},
                           'fixture': fixture},
            'market_result': {'fixture': fixture, 'markets': rows, 'refresh': {'attempted': False},
                'market_filter': {'market_kind': 'handicap', 'period': 'full_time'},
                'market_coverage': {'total_quotes': count, 'matched_quotes': count,
                                   'returned_quotes': count, 'complete_for_filter': True}},
            'record_result': {'accepted': [{'index': 0, 'fixture_id': fixture['id'],
                                          'signal_id': f'signal-{index}', 'duplicate': False}],
                              'rejected': []}, **updates}

    async def batch(self, items):
        response = await self.rpc('tools/call', {'name': 'review_match_batch',
            'arguments': {'review_id': 'job', 'attempt_id': 'attempt',
                          'items': [item.model_dump(mode='json') for item in items]}})
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()['result']
        self.assertFalse(result.get('isError', False), result)
        text = result['content'][0]['text']
        return json.loads(text), text

    async def test_six_legal_120_quote_inventories_fit_and_keep_all_native_states_and_receipts(self):
        native = [self.native(index) for index in range(6)]
        for result in native:
            self.assertLess(len(json.dumps(result['market_result'], ensure_ascii=False, indent=2)), 90000)
        before = copy.deepcopy(native)
        async def review(job, attempt, item):
            return native[int(item.team_a[-1])]
        with patch.object(self.server, '_review_batch_item', side_effect=review):
            result, text = await self.batch([self.item(index) for index in range(6)])
        self.assertLess(len(text), 90000)
        self.assertEqual(before, native)
        for index, entry in enumerate(result['items']):
            view = entry['result']
            self.assertEqual(native[index]['record_result'], view['record_result'])
            market = view['market_result']
            self.assertNotIn('markets', market)
            self.assertNotIn('market_coverage', market)
            self.assertEqual(native[index]['market_result']['market_coverage'], market['native_market_coverage'])
            self.assertEqual(native[index]['market_result']['markets'], restore_rows(market['source_selection_quotes']))
            self.assertTrue(market['presentation']['all_source_selection_states'])
            self.assertEqual({'active', 'closed', 'suspended'},
                             {row['status'] for row in restore_rows(market['source_selection_quotes'])})

    async def test_six_json_failure_receipts_remain_structured_unknown_and_under_budget(self):
        async def review(job, attempt, item):
            receipt = self.native(int(item.team_a[-1]))
            del receipt['record_result']
            raise self.server.ToolError(json.dumps({'completed_receipt': receipt,
                'failed_operation': 'record_expert_signals', 'record_result_verified': False,
                'error': '系统工具超时'}, ensure_ascii=False))
        with patch.object(self.server, '_review_batch_item', side_effect=review):
            result, text = await self.batch([self.item(index) for index in range(6)])
        self.assertLess(len(text), 90000)
        for index, entry in enumerate(result['items']):
            self.assertTrue(entry['unresolved'])
            self.assertTrue(entry['native_outcome_unknown'])
            self.assertFalse(entry['record_result_verified'])
            self.assertFalse(entry['retry_without_native_evidence'])
            receipt = entry['completed_receipt']
            self.assertEqual(f'{index:064x}', receipt['search_key'])
            self.assertIsNone(receipt['next_cursor'])
            self.assertNotIn('record_result', receipt)
            self.assertEqual(120, len(restore_rows(receipt['market_result']['source_selection_quotes'])))

    async def test_source_line_view_uses_selected_side_line_and_preserves_rejections(self):
        native = self.native(count=3)
        native['market_result']['markets'].append(native['market_result']['markets'][0] |
            {'quote_id': 'different-line', 'selection_line': '0.25'})
        native['market_result']['market_coverage'].update(total_quotes=4, matched_quotes=4, returned_quotes=4)
        native['record_result'] = {'accepted': [], 'rejected': [{'index': 0, 'reason': 'quote_closed'}]}
        view = self.server._batch_result_view(native, self.item())
        rows = restore_rows(view['market_result']['source_selection_quotes'])
        self.assertEqual(3, len(rows))
        self.assertTrue(all(row['selection_line'] == '-0.25' and row['line'] == '0.25' for row in rows))
        self.assertEqual(native['record_result'], view['record_result'])

    async def test_total_query_and_unknown_other_market_are_explicitly_different_presentations(self):
        native = self.native(count=3)
        native['market_result']['market_filter'] = {'market_kind': 'total', 'period': 'full_time',
                                                   'selection': 'over', 'line': '2.75'}
        for row in native['market_result']['markets']:
            row.update(market_kind='total', selection='over', line='2.75', selection_line='2.75', line_basis='selection')
        total = self.item(market_kind='total', market_query='大2.5/3', selection_team=None, selection_line=None)
        view = self.server._batch_result_view(native, total)
        self.assertEqual(3, len(restore_rows(view['market_result']['source_selection_quotes'])))
        self.assertEqual('2.75', view['market_result']['presentation']['selection_line'])
        unknown = self.item(market_kind='winner', selection_team=None, selection_line=None)
        view = self.server._batch_result_view(native, unknown)
        self.assertTrue(view['market_result']['presentation']['requires_manual_market_review'])
        self.assertFalse(view['market_result']['presentation']['all_source_selection_states'])
        self.assertEqual([], view['market_result']['source_selection_quotes'])

    async def test_partial_native_scope_or_unknown_orientation_never_claims_all_selected_states(self):
        for change in ('coverage', 'orientation'):
            with self.subTest(change=change):
                native = self.native(count=3)
                if change == 'coverage':
                    native['market_result']['market_coverage']['complete_for_filter'] = False
                else:
                    native['resolution']['source_team_sides'] = None
                view = self.server._batch_result_view(native, self.item())
                self.assertFalse(view['market_result']['presentation']['all_source_selection_states'])
                self.assertTrue(view['market_result']['presentation']['requires_manual_market_review'])
                self.assertEqual([], view['market_result']['source_selection_quotes'])

    async def test_large_candidates_are_explicit_partial_and_all_disambiguation_candidates_remain(self):
        native = self.native()
        candidates = [{'id': f'candidate-{index}', 'home_team': 'Alpha', 'away_team': 'Beta',
            'competition_name': f'Competition-{index}', 'sport': 'football', 'phase': 'scheduled',
            'kickoff_at': '2026-11-12T12:00:00Z'} for index in range(2000)]
        native.update(items=candidates, total_candidates=2000,
            resolution={'resolved': False, 'reason': 'ambiguous_fixture', 'candidate_count': 20,
                        'matching_candidates': candidates[:20], 'matching_candidates_complete': True})
        del native['market_result'], native['record_result']
        view = self.server._batch_result_view(native, self.item())
        self.assertEqual(2000, view['presentation']['candidates']['native_returned_count'])
        self.assertEqual(3, view['presentation']['candidates']['displayed_count'])
        self.assertFalse(view['presentation']['candidates']['items_complete'])
        self.assertTrue(view['presentation']['candidates']['native_search_complete'])
        self.assertEqual(candidates[:20], restore_rows(view['resolution']['matching_candidates']))

    async def test_search_error_keeps_native_next_cursor_and_does_not_claim_record_unknown(self):
        receipt = {'search_key': 'a' * 64, 'next_cursor': 'native-next-cursor',
                   'search_complete': False, 'total_candidates': 109, 'items': []}
        error = self.server.ToolError(json.dumps({'completed_receipt': receipt,
            'failed_operation': 'search_matches', 'error': 'temporary rejection'}))
        view = self.server._batch_error_view(error, self.item())
        self.assertEqual('native-next-cursor', view['completed_receipt']['next_cursor'])
        self.assertFalse(view['completed_receipt']['search_complete'])
        self.assertFalse(view['native_outcome_unknown'])
        self.assertFalse(view['retry_without_native_evidence'])

    async def test_one_protocol_failure_cannot_hide_another_committed_receipt_or_leak_body(self):
        async def review(job, attempt, item):
            if item.team_a == 'Alpha0':
                return self.native(0)
            await asyncio.sleep(0)
            raise json.JSONDecodeError('private-upstream-body', 'private-upstream-body', 0)
        with patch.object(self.server, '_review_batch_item', side_effect=review):
            result, text = await self.batch([self.item(0), self.item(1)])
        self.assertEqual('signal-0', result['items'][0]['result']['record_result']['accepted'][0]['signal_id'])
        failed = result['items'][1]
        self.assertEqual('JSONDecodeError', failed['error_class'])
        self.assertTrue(failed['protocol_failure'])
        self.assertTrue(failed['native_outcome_unknown'])
        self.assertFalse(failed['retry_without_native_evidence'])
        self.assertNotIn('private-upstream-body', text)

    async def test_extreme_display_is_withheld_explicitly_without_hiding_committed_receipt(self):
        native = self.native(count=300)
        for index, row in enumerate(native['market_result']['markets']):
            row['market_name'] = f'盘口-{index}-' + 'x' * 120
        view = self.server._batch_result_view(native, self.item())
        self.assertLess(len(json.dumps(view, ensure_ascii=False, separators=(',', ':'))), 14400)
        market = view['market_result']
        self.assertEqual(300, market['native_counts']['source_selection_quotes'])
        self.assertEqual(0, market['native_counts']['displayed_source_selection_quotes'])
        self.assertTrue(market['presentation']['withheld_for_inline_budget'])
        self.assertTrue(market['presentation']['requires_manual_market_review'])
        self.assertFalse(market['presentation']['all_source_selection_states'])
        self.assertEqual(native['record_result'], view['record_result'])

