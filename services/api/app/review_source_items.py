"""Negative coverage check for explicit text items omitted by the agent.

A read scope proves only that an item was considered. It cannot prove which
fixture the source means, select a price, or authorize a recommendation.
"""
from decimal import Decimal, InvalidOperation


def _covers(payload: dict, item: dict) -> bool:
    coverage = payload.get('market_coverage') or {}
    scope = payload.get('market_filter')
    if not isinstance(scope, dict) or coverage.get('complete_for_filter') is not True:
        return False
    if scope.get('market_kind') not in (None, 'total'):
        return False
    if any(scope.get(key) not in (None, item[key]) for key in ('period', 'selection')):
        return False
    if scope.get('market_query'):
        return False  # Unsupported free text is not complete numerical coverage.
    if scope.get('line') is not None:
        try:
            return Decimal(str(scope['line'])) == Decimal(item['line'])
        except (InvalidOperation, ValueError, TypeError):
            return False
    return True


async def source_total_review_gaps(conn, job_id, attempt_id, items: list[dict]) -> list[dict]:
    if not items:
        return []
    rows = await conn.fetch(
        "SELECT t.payload FROM v2_analysis_traces t "
        "WHERE t.job_id=$1 AND t.payload->>'attempt_id'=($2::uuid)::text "
        "AND t.trace_kind='mcp_list_match_markets' AND t.payload->>'status'='succeeded' "
        "AND EXISTS(SELECT 1 FROM v2_review_fixture_bindings b "
        "WHERE b.job_id=$1 AND b.attempt_id=$2 AND b.fixture_id=t.payload->>'fixture_id') "
        "ORDER BY t.sequence_no LIMIT 101", job_id, attempt_id)
    # Do not claim completeness after truncating a pathological attempt.
    if len(rows) > 100:
        return [{'reason': 'source_total_read_budget_exceeded'}]
    return [item for item in items
            if item.get('reason') or not any(_covers(row['payload'], item) for row in rows)]
