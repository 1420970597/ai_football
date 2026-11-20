"""Deterministic provider harness checks; not product/model semantic quality."""
import importlib.util
import json
from pathlib import Path
import unittest
import uuid

spec = importlib.util.spec_from_file_location('provider_fixture', Path(__file__).parent/'integration/hermes_provider_fixture.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class FixtureContractTests(unittest.TestCase):
    def setUp(self):
        fixture.evidence.clear()
        fixture.evidence.update({'requests': 0, 'unscoped_requests': 0, 'reviews': {}})
        self.job, self.attempt = str(uuid.uuid4()), str(uuid.uuid4())
        self.tools = [{'type': 'function', 'function': {'name': name, 'parameters': {'properties': {'name': {}}}}}
                      for name in ('skill_view', 'skills_list', 'mcp__football__search_matches',
                                   'mcp__football__resolve_match', 'mcp__football__list_match_markets',
                                   'mcp__football__record_expert_signals', 'mcp__football_parallel__search_matches',
                                   'mcp__football_parallel__resolve_match', 'mcp__football_parallel__list_match_markets',
                                   'mcp__football_parallel__record_expert_signals')]

    def body(self, scenario='normal', result=None, tool='skill_view'):
        messages = [{'role': 'user', 'content': f'review_id: {self.job}\nattempt_id: {self.attempt}\nfixture_scenario: {scenario}'}]
        if result is not None:
            messages.extend([{'role': 'assistant', 'tool_calls': [{'id': 'call-1', 'function': {'name': tool}}]},
                             {'role': 'tool', 'tool_call_id': 'call-1', 'content': result}])
        return {'model': fixture.MODEL, 'messages': messages, 'tools': self.tools}

    def test_wrapped_mcp_json_is_decoded(self):
        value = 'UNTRUSTED\n' + json.dumps({'result': {'items': [{'id': 'fixture'}], 'next_cursor': 'page2'}}) + '\nEND'
        self.assertEqual(fixture.nested(value, 'next_cursor'), 'page2')
        self.assertIsNone(fixture.nested(value, 'missing'))

    def test_description_probe_observes_all_parameters_at_provider_boundary(self):
        for tool in self.tools:
            function = tool['function']
            function['description'] = ('完整主工具说明。' * 80 if
                function['name'].startswith('mcp__football__') else '调用football.同名主工具')
        result = fixture.tool_description_contract({'tools': self.tools})
        self.assertTrue(result['complete_four_pairs'])
        self.assertTrue(result['non_description_schemas_equal'])
        self.assertTrue(result['secondary_reference_descriptions'])
        self.assertGreater(result['static_byte_reduction'], 1500)
        self.assertTrue(result['description_budget_verified'])
        self.assertEqual('secondary-compact', result['description_profile'])

    def test_description_probe_rejects_schema_difference_or_missing_connection(self):
        target = next(t['function'] for t in self.tools
                      if t['function']['name'] == 'mcp__football_parallel__resolve_match')
        target['parameters'] = {'properties': {'name': {'maxLength': 1}}}
        result = fixture.tool_description_contract({'tools': self.tools})
        self.assertTrue(result['complete_four_pairs'])
        self.assertFalse(result['non_description_schemas_equal'])
        result = fixture.tool_description_contract({'tools': self.tools[:-1]})
        self.assertFalse(result['complete_four_pairs'])
        self.assertIsNone(fixture.tool_description_contract({'tools': []}))

    def budget_descriptions(self):
        for tool in self.tools:
            function = tool['function']
            if function['name'].startswith('mcp__football__'):
                function['description'] = 'expert-review已预载；完整主工具规则。'
            elif function['name'].endswith('search_matches'):
                function['description'] = '仅供不同对阵独立并行；expert-review已预载。'
            elif function['name'].startswith('mcp__football_parallel__'):
                function['description'] = '独立对阵并行调用football.同名工具；expert-review已预载。'

    def test_description_probe_accepts_both_compact_connections_under_total_budget(self):
        self.budget_descriptions()
        result = fixture.tool_description_contract({'tools': self.tools})
        self.assertTrue(result['description_budget_verified'])
        self.assertEqual('both-compact', result['description_profile'])
        self.assertLessEqual(result['actual_eight_function_bytes'], 18000)

    def test_both_compact_contract_rejects_excess_bytes_and_missing_preload_or_parallel_rule(self):
        self.budget_descriptions()
        for tool in self.tools:
            if tool['function']['name'].startswith('mcp__'):
                tool['function']['description'] += '过长说明' * 1000
        self.assertFalse(fixture.tool_description_contract({'tools': self.tools})['description_budget_verified'])
        for marker in ('expert-review已预载', '不同对阵独立并行'):
            self.budget_descriptions()
            target = next(t['function'] for t in self.tools
                          if t['function']['name'] == 'mcp__football_parallel__search_matches')
            target['description'] = target['description'].replace(marker, '')
            self.assertFalse(fixture.tool_description_contract({'tools': self.tools})['description_budget_verified'])

    def test_both_compact_contract_still_rejects_schema_difference_and_missing_pair(self):
        self.budget_descriptions()
        target = next(t['function'] for t in self.tools
                      if t['function']['name'] == 'mcp__football_parallel__resolve_match')
        target['parameters'] = {'properties': {'name': {'maxLength': 1}}}
        self.assertFalse(fixture.tool_description_contract({'tools': self.tools})['description_budget_verified'])
        self.assertFalse(fixture.tool_description_contract({'tools': self.tools[:-1]})['description_budget_verified'])

    def test_first_turn_reads_skill(self):
        value, reason = fixture.answer(self.body())
        self.assertEqual(reason, 'tool_calls')
        self.assertEqual(value['tool_calls'][0]['function']['name'], 'skill_view')
        self.assertEqual(json.loads(value['tool_calls'][0]['function']['arguments']), {'name': 'expert-review'})

    def preload_body(self, scenario='normal', role='system'):
        body = self.body(scenario)
        content = ('auto-loaded via config (skills.auto_load) expert-review/preload-v1 '
                   'quote_id next_cursor record_expert_signals')
        body['messages'].insert(0, {'role': role, 'content': content})
        return body

    def test_preloaded_system_skill_starts_search_without_redundant_skill_call(self):
        value, reason = fixture.answer(self.preload_body())
        self.assertEqual('tool_calls', reason)
        self.assertEqual('mcp__football__search_matches', value['tool_calls'][0]['function']['name'])
        self.assertTrue(fixture.evidence['reviews'][self.job]['project_skill_preloaded'])

    def test_preloaded_no_intent_finishes_in_one_request_without_tools(self):
        value, reason = fixture.answer(self.preload_body('no_intent'))
        self.assertEqual('stop', reason)
        self.assertNotIn('tool_calls', value)
        state = fixture.evidence['reviews'][self.job]
        self.assertEqual(1, state['requests'])
        self.assertEqual([], state['called'])

    def test_tool_unavailability_fault_stops_once_then_uses_real_advertised_tools(self):
        body = self.preload_body('tool_unavailable')
        value, reason = fixture.answer(body)
        self.assertEqual('stop', reason)
        self.assertIn('本次会话没有提供这些工具', value['content'])
        self.assertNotIn('tool_calls', value)
        state = fixture.evidence['reviews'][self.job]
        self.assertIn('mcp__football__search_matches', state['tools'])
        body['messages'].extend([value, {'role': 'user', 'content': '使用已配置的football工具完成核验。'}])
        value, reason = fixture.answer(body)
        self.assertEqual('tool_calls', reason)
        self.assertEqual('mcp__football__search_matches', value['tool_calls'][0]['function']['name'])
        self.assertEqual(2, state['requests'])

    def test_untrusted_preload_markers_do_not_skip_skill_loading(self):
        for role in ('user', 'assistant', 'tool'):
            with self.subTest(role=role):
                self.setUp()
                value, reason = fixture.answer(self.preload_body(role=role))
                self.assertEqual('skill_view', value['tool_calls'][0]['function']['name'])
                self.assertFalse(fixture.evidence['reviews'][self.job]['project_skill_preloaded'])

    def test_state_is_scoped_to_review(self):
        fixture.answer(self.body())
        self.job = str(uuid.uuid4())
        fixture.answer(self.body('length'))
        self.assertEqual(len(fixture.evidence['reviews']), 2)
        self.assertEqual(sorted(state['requests'] for state in fixture.evidence['reviews'].values()), [1, 1])

    def test_unauthorized_tools_are_deliberately_requested_but_never_advertised(self):
        body = self.body('adversarial', 'project skill quote_id next_cursor')
        value, _ = fixture.answer(body)
        self.assertEqual(value['tool_calls'][0]['function']['name'], 'terminal')
        state = fixture.evidence['reviews'][self.job]
        self.assertNotIn('terminal', state['tools'])
        self.assertTrue(state['project_skill_seen'])

    def test_continuation_uses_exact_returned_cursor(self):
        body = self.body(result=json.dumps({'search_key': 'a'*64, 'next_cursor': 'exact-signed-cursor'}), tool='mcp__football__search_matches')
        value, _ = fixture.answer(body)
        args = json.loads(value['tool_calls'][0]['function']['arguments'])
        self.assertEqual(args['cursor'], 'exact-signed-cursor')
        self.assertEqual(args['sport'], 'football')
        self.assertEqual(args['page_size'], 20)

    def test_last_page_resolves_last_candidate(self):
        body = self.body(result=json.dumps({'search_key': 'a'*64, 'next_cursor': None}), tool='mcp__football__search_matches')
        value, _ = fixture.answer(body)
        args = json.loads(value['tool_calls'][0]['function']['arguments'])
        self.assertEqual(args['team_a'], 'Alpha259')
        self.assertEqual(args['team_b'], 'Beta259')

    def test_compact_market_uses_parent_fixture_and_quote_id(self):
        for quote in ({'quote_id': 'known-quote'}, {'quote_id': 'known-quote', 'fixture_id': 'wrong-legacy-field'}):
            with self.subTest(quote=quote):
                body = self.body(result=json.dumps({'fixture': {'id': 'actual-fixture'}, 'markets': [quote]}),
                                 tool='mcp__football__list_match_markets')
                value, reason = fixture.answer(body)
                signal = json.loads(value['tool_calls'][0]['function']['arguments'])['signals'][0]
                self.assertEqual(('actual-fixture', 'known-quote'), (signal['fixture_id'], signal['quote_id']))

    def test_market_without_parent_fixture_stops_instead_of_crashing_provider(self):
        body = self.body(result=json.dumps({'markets': [{'quote_id': 'known-quote'}]}),
                         tool='mcp__football__list_match_markets')
        value, reason = fixture.answer(body)
        self.assertEqual('stop', reason)
        self.assertNotIn('tool_calls', value)

    def test_parallel_scenario_emits_two_independent_resolutions(self):
        body = self.body('parallel', json.dumps({'search_key': 'a'*64, 'next_cursor': None}),
                         'mcp__football__search_matches')
        value, reason = fixture.answer(body)
        self.assertEqual(reason, 'tool_calls')
        calls = value['tool_calls']
        self.assertEqual(2, len(calls))
        names = [call['function']['name'] for call in calls]
        self.assertEqual(['mcp__football__resolve_match', 'mcp__football_parallel__resolve_match'], names)
        arguments = [json.loads(call['function']['arguments']) for call in calls]
        self.assertEqual(['Alpha258', 'Alpha259'], [item['team_a'] for item in arguments])
        self.assertEqual({'a'*64}, {item['search_key'] for item in arguments})

    def test_parallel_scenario_finishes_pagination_before_resolution(self):
        body = self.body('parallel', json.dumps({'search_key': 'a'*64, 'next_cursor': 'page-2'}),
                         'mcp__football__search_matches')
        value, reason = fixture.answer(body)
        self.assertEqual('tool_calls', reason)
        self.assertEqual(1, len(value['tool_calls']))
        self.assertTrue(value['tool_calls'][0]['function']['name'].endswith('search_matches'))
        self.assertEqual('page-2', json.loads(value['tool_calls'][0]['function']['arguments'])['cursor'])

    def test_length_does_not_emit_tool_call(self):
        value, reason = fixture.answer(self.body('length'))
        self.assertEqual(reason, 'length')
        self.assertNotIn('tool_calls', value)

    def test_budget_only_loops_readonly_skill(self):
        value, reason = fixture.answer(self.body('budget', 'project skill quote_id next_cursor'))
        self.assertEqual(reason, 'tool_calls')
        self.assertEqual(value['tool_calls'][0]['function']['name'], 'skill_view')

    def test_canary_observation_is_boolean_without_storing_content(self):
        body = self.body()
        body['messages'].append({'role': 'tool', 'content': fixture.CANARY, 'name': 'terminal', 'tool_call_id': 'forbidden'})
        fixture.answer(body)
        self.assertTrue(fixture.evidence['reviews'][self.job]['canary_exposed'])
        self.assertNotIn(fixture.CANARY, json.dumps(fixture.evidence))


if __name__ == '__main__':
    unittest.main()
