import json
import unittest

from app.admin_tasks import diagnostic_tasks


class AdminTaskTests(unittest.TestCase):
    def test_current_observations_have_specific_actions_and_no_invented_failure_history(self):
        result = diagnostic_tasks({
            'collector': {'state': 'reachable', 'authorized': True, 'auth_state': 'authorized',
                          'sources': {'leyu': {'last_success_at': '2026-10-27T00:00:00Z', 'last_error': ''}}},
            'schema': 'verified',
            'pending_media': {'count': 2, 'capped': False},
            'unresolved_reviews': {'count': 5000, 'capped': True},
            'stale_quotes': {'count': 0, 'capped': False},
            'unknown_orders': {'count': 1, 'capped': False},
        })
        tasks = {item['key']: item for item in result}
        self.assertEqual(8, len(tasks))
        self.assertEqual('clear', tasks['telegram_session']['state'])
        self.assertEqual('clear', tasks['leyu_session']['state'])
        self.assertEqual('action_required', tasks['pending_media']['state'])
        self.assertEqual({'count': 5000, 'capped': True}, tasks['unresolved_reviews']['count'])
        self.assertEqual('orders', tasks['unknown_orders']['target'])
        self.assertIn('禁止重复提交', tasks['unknown_orders']['detail'])
        self.assertEqual('clear', tasks['schema_migration']['state'])
        self.assertEqual('unverified', tasks['migration_failures']['state'])

    def test_unknown_or_invalid_observation_cannot_be_rendered_as_zero_or_healthy(self):
        tasks = {item['key']: item for item in diagnostic_tasks({
            'pending_media': {'count': True}, 'unknown_orders': {'count': 5001},
            'stale_quotes': {'count': -1}, 'collector': {'sources': {}},
        })}
        for task in tasks.values():
            self.assertEqual('unverified', task['state'])
            self.assertNotIn('count', task)

    def test_rejected_sessions_and_schema_provide_actions_without_raw_secrets(self):
        result = diagnostic_tasks({
            'collector': {'state': 'reachable', 'authorized': False, 'auth_state': 'session_invalid',
                'account': {'secret': 'never-expose-this'},
                'sources': {'leyu': {'failure_group': 'authorization', 'last_error': 'raw-sensitive-value'}}},
            'schema': 'migration_required',
        })
        tasks = {item['key']: item for item in result}
        for key in ('telegram_session', 'leyu_session', 'schema_migration'):
            self.assertEqual('action_required', tasks[key]['state'])
        self.assertNotIn('never-expose-this', json.dumps(result))
        self.assertNotIn('raw-sensitive-value', json.dumps(result))
