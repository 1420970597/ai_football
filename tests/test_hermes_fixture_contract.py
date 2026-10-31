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

    def test_first_turn_reads_skill(self):
        value, reason = fixture.answer(self.body())
        self.assertEqual(reason, 'tool_calls')
        self.assertEqual(value['tool_calls'][0]['function']['name'], 'skill_view')
        self.assertEqual(json.loads(value['tool_calls'][0]['function']['arguments']), {'name': 'expert-review'})

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
