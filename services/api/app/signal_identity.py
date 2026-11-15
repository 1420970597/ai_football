"""Deterministic betting intent; price and Telegram wording are not identity."""

DUPLICATE_REASON = 'duplicate_recommendation'


def baseline_sql(alias):
    # An unknown live rule cannot silently be treated as full-period. Only the
    # relevant period's score is identity; missing scores remain unknown.
    return (f"CASE WHEN {alias}.phase='live' AND {alias}.score_basis<>'full_period' THEN "
            f"CASE WHEN {alias}.period='first_half' THEN jsonb_build_array("
            f"{alias}.score_baseline->'home',{alias}.score_baseline->'away',"
            f"{alias}.score_baseline->'half_home',{alias}.score_baseline->'half_away') "
            f"WHEN {alias}.period='second_half' THEN {alias}.score_baseline ELSE "
            f"jsonb_build_array({alias}.score_baseline->'home',{alias}.score_baseline->'away') END "
            "ELSE 'null'::jsonb END")


def identity_columns(alias):
    return ','.join([f'{alias}.{key}' for key in
                    ('fixture_id', 'market_id', 'market_kind', 'period', 'selection', 'phase', 'score_basis')]
                   + [f'COALESCE({alias}.line,0)', baseline_sql(alias)])


def same_intent_sql(left, right):
    return (f"ROW({identity_columns(left)}) IS NOT DISTINCT FROM "
            f"ROW({identity_columns(right)})")


def first_expert_intent_sql(alias='s'):
    """Keep the first expert intent even outside a selected window or cycle.

    Price, outcome, message edits and channel changes cannot choose a different
    winner. An intent explicitly corrected as invalid cannot hide a valid pick.
    Missing quote/settlement evidence stays missing instead of borrowing a later
    duplicate's better price or result.
    """
    from .review_period_intent import INTENT_VALID_SQL
    prior = 'canonical_prior'
    valid = INTENT_VALID_SQL.replace('s.', prior + '.')
    return (f"COALESCE({alias}.withdrawal_reason,'')<>'{DUPLICATE_REASON}' "
            f"AND NOT EXISTS(SELECT 1 FROM v2_expert_signals {prior} "
            f"WHERE {prior}.expert_id={alias}.expert_id AND {prior}.fixture_id={alias}.fixture_id "
            f"AND ({prior}.created_at,{prior}.id)<({alias}.created_at,{alias}.id) "
            f"AND COALESCE({prior}.withdrawal_reason,'')<>'{DUPLICATE_REASON}' "
            f"AND {valid} AND " + same_intent_sql(prior, alias) + ')')


async def previous_intent_order(conn, user_id, account_id, signal_id, *, excluding=None):
    """Caller holds the user's account lock, including during execution checks."""
    return await conn.fetchrow(
        "SELECT o.* FROM v2_bet_orders o JOIN v2_expert_signals previous ON previous.id=o.signal_id "
        "JOIN v2_expert_signals current ON current.id=$3 "
        "WHERE o.user_id=$1 AND o.account_id=$2 AND " + same_intent_sql('previous', 'current') +
        " AND ($4::uuid IS NULL OR o.id<>$4 AND (o.state IN('submitting','unknown','accepted','settled') "
        "OR (o.created_at,o.id)<(SELECT created_at,id FROM v2_bet_orders WHERE id=$4))) "
        "ORDER BY o.created_at,o.id LIMIT 1", user_id, account_id, signal_id, excluding)
