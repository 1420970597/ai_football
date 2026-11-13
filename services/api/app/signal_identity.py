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


async def previous_intent_order(conn, user_id, account_id, signal_id, *, excluding=None):
    """Caller holds the user's account lock, including during execution checks."""
    return await conn.fetchrow(
        "SELECT o.* FROM v2_bet_orders o JOIN v2_expert_signals previous ON previous.id=o.signal_id "
        "JOIN v2_expert_signals current ON current.id=$3 "
        "WHERE o.user_id=$1 AND o.account_id=$2 AND " + same_intent_sql('previous', 'current') +
        " AND ($4::uuid IS NULL OR o.id<>$4 AND (o.state IN('submitting','unknown','accepted','settled') "
        "OR (o.created_at,o.id)<(SELECT created_at,id FROM v2_bet_orders WHERE id=$4))) "
        "ORDER BY o.created_at,o.id LIMIT 1", user_id, account_id, signal_id, excluding)
