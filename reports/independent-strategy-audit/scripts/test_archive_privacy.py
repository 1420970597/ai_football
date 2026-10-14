#!/usr/bin/env python3
"""Local, standard-library checks for public archive export transformations."""
import unittest

from archive_privacy import (
    POST_GOAL_FIELDS,
    REAL_ORDER_FIELDS,
    pseudonymize_match_id,
    redact_post_goal_pick,
    redact_real_order,
)


class ArchivePrivacyTests(unittest.TestCase):
    def order(self):
        row = {key: '0' for key in REAL_ORDER_FIELDS}
        row.update(match_id='1234567', stake='3.50', decision_odds='1.92',
                   sim_status='half_won', sim_pnl_per_unit='.46',
                   order_time='2026-10-12T19:01:02Z', league='Example (8分钟)')
        del row['is_virtual']
        return row

    def test_orders_keep_values_and_match_groups_without_time_or_names(self):
        raw = self.order()
        cleaned = redact_real_order(raw)
        self.assertEqual(tuple(cleaned), REAL_ORDER_FIELDS)
        self.assertNotIn('order_time', cleaned)
        self.assertNotIn('league', cleaned)
        self.assertEqual(cleaned['stake'], '3.50')
        self.assertEqual(cleaned['sim_pnl_per_unit'], '.46')
        self.assertEqual(cleaned['is_virtual'], 'true')
        self.assertEqual(redact_real_order(cleaned), cleaned)
        self.assertEqual(cleaned['match_id'], pseudonymize_match_id(raw['match_id']))
        raw['league'] = 'Example league'
        self.assertEqual(redact_real_order(raw)['is_virtual'], 'false')

    def test_post_goal_uses_same_alias_and_keeps_relative_timing(self):
        raw = {key: '0' for key in POST_GOAL_FIELDS}
        raw.update(match_id='1234567', published_at_ms='1700000000000',
                   secs_since_score_change='24.5', clock='41:52')
        cleaned = redact_post_goal_pick(raw)
        self.assertNotIn('published_at_ms', cleaned)
        self.assertEqual(cleaned['secs_since_score_change'], '24.5')
        self.assertEqual(cleaned['clock'], '41:52')
        self.assertEqual(cleaned['match_id'], redact_real_order(self.order())['match_id'])

    def test_empty_exports_remain_empty(self):
        self.assertEqual([redact_real_order(row) for row in []], [])
        self.assertEqual([redact_post_goal_pick(row) for row in []], [])

    def test_empty_identifier_and_invalid_virtual_flag_fail_explicitly(self):
        for empty in ('', '   ', None):
            with self.assertRaises(ValueError):
                pseudonymize_match_id(empty)
        raw = self.order()
        raw['is_virtual'] = 'unknown'
        with self.assertRaises(ValueError):
            redact_real_order(raw)


if __name__ == '__main__':
    unittest.main()
