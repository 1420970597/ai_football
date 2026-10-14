#!/usr/bin/env python3
"""Pseudonymize report exports without changing their numerical observations.

These public, deterministic aliases preserve match clusters across exports.
They are not a promise of irreversible anonymization or protection against
reidentification from public match details.
"""
import hashlib
import re


REAL_ORDER_FIELDS = (
    'match_id', 'market', 'line', 'outcome', 'stake', 'decision_odds',
    'p_fused', 'sim_status', 'sim_pnl_per_unit', 'is_virtual',
    'config_version', 'quote_to_submit_ms',
)
POST_GOAL_FIELDS = (
    'match_id', 'clock', 'score', 'secs_since_score_change', 'market',
    'line', 'outcome', 'odds', 'p_model', 'p_market',
)


def pseudonymize_match_id(value):
    """Return the same public alias for the same ID, preserving existing aliases."""
    match_id = str(value).strip() if value is not None else ''
    if not match_id:
        raise ValueError('match_id must not be empty')
    if re.fullmatch(r'match_[0-9a-f]{24}', match_id):
        return match_id
    return 'match_' + hashlib.sha256(match_id.encode('utf-8')).hexdigest()[:24]


def redact_real_order(row):
    """Omit absolute trade time and names; retain the historical virtual split."""
    cleaned = {key: row[key] for key in REAL_ORDER_FIELDS if key != 'is_virtual'}
    cleaned['match_id'] = pseudonymize_match_id(row['match_id'])
    if 'is_virtual' in row:
        virtual = str(row['is_virtual']).strip().casefold()
        if virtual not in {'true', 'false'}:
            raise ValueError('is_virtual must be true or false')
        cleaned['is_virtual'] = virtual
    else:
        cleaned['is_virtual'] = str('分钟' in row['league']).lower()
    return {key: cleaned[key] for key in REAL_ORDER_FIELDS}


def redact_post_goal_pick(row):
    """Omit the absolute publish time while retaining relative timing fields."""
    cleaned = {key: row[key] for key in POST_GOAL_FIELDS}
    cleaned['match_id'] = pseudonymize_match_id(row['match_id'])
    return cleaned
