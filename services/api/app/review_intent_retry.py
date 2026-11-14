"""Negative completion hints for rejected IDs when exact options were read.

Nothing here chooses or records a quote. Recording still checks its current
price, source time, score context, source intent, lease and authorization.
"""
from decimal import Decimal, InvalidOperation

from .handicap_lines import line_terms
from .review_period_intent import INTENT_VALID_SQL


def intent_matches_quote(context, quote):
    if not isinstance(context, dict):
        return False
    if any(str(quote.get(key)) != context.get(key)
           for key in ('fixture_id', 'market_id', 'market_kind', 'period', 'selection')):
        return False
    intent = context.get('source_intent')
    if not isinstance(intent, dict) or quote.get('line') is None:
        return False
    try:
        if context['market_kind'] == 'handicap':
            return Decimal(line_terms(quote)['selection_line']) == Decimal(intent['selection_line'])
        if context['market_kind'] == 'total' and context['selection'] == intent.get('selection'):
            return Decimal(str(quote['line'])) in {Decimal(value) for value in intent.get('lines', [])}
    except (InvalidOperation, TypeError, KeyError):
        return False
    return False


def _numeric(expression):
    # Only server-written canonical, finite quarter-lines are cast. Corrupt or
    # legacy trace fields remain non-matching instead of breaking completion.
    return f"(CASE WHEN {expression} ~ '^-?[0-9]{{1,3}}(\\.[0-9]{{1,3}})?$' THEN ({expression})::numeric END)"


def _matches(alias, context):
    scope = ' AND '.join(f"{alias}.{key}={context}->>'{key}'"
                         for key in ('fixture_id', 'market_id', 'market_kind', 'period', 'selection'))
    selected_line = (f"CASE WHEN {alias}.fixture_id ~ '^leyu:[0-9]+$' "
                     f"AND {alias}.market_id IN('4','19','39') AND {alias}.selection='away' "
                     f"THEN -{alias}.line ELSE {alias}.line END")
    wanted = _numeric(f"({context}->'source_intent'->>'selection_line')")
    alternative = _numeric('wanted.value')
    return (f"({scope} AND CASE WHEN {alias}.market_kind='handicap' THEN ({selected_line})={wanted} "
            f"WHEN {alias}.market_kind='total' AND {alias}.selection={context}->'source_intent'->>'selection' "
            f"THEN EXISTS(SELECT 1 FROM jsonb_array_elements_text(CASE "
            f"WHEN jsonb_typeof({context}->'source_intent'->'lines')='array' "
            f"THEN {context}->'source_intent'->'lines' ELSE '[]'::jsonb END) wanted(value) "
            f"WHERE {alias}.line={alternative}) ELSE false END) ")


def intent_retry_ctes():
    """Append to the existing single completion read with identical parameters."""
    return (
        "line_failures AS MATERIALIZED (SELECT DISTINCT (r.value->'intent_context')-'selected_selection_line' AS context "
        "FROM v2_analysis_traces t CROSS JOIN LATERAL jsonb_array_elements(t.payload->'rejected') r(value) "
        "WHERE t.job_id=$1 AND t.payload->>'attempt_id'=($2::uuid)::text "
        "AND t.trace_kind='mcp_record_expert_signals' AND t.payload->>'status'='succeeded' "
        "AND r.value->>'reason' IN('source_line_intent_conflict','source_selection_intent_conflict') "
        "AND jsonb_typeof(r.value->'intent_context')='object'),"
        "intent_retries AS (SELECT f.context,options.quote_ids FROM line_failures f "
        "CROSS JOIN LATERAL (SELECT jsonb_agg(x.id ORDER BY x.id) AS quote_ids FROM ("
        "SELECT q.id FROM v2_review_market_reads r JOIN v2_current_quotes q ON q.id=r.quote_id "
        "JOIN v2_fixtures fixture ON fixture.id=q.fixture_id "
        "JOIN v2_review_fixture_bindings b ON b.job_id=r.job_id AND b.attempt_id=r.attempt_id "
        "AND b.fixture_id=q.fixture_id "
        "WHERE r.job_id=$1 AND r.attempt_id=$2 AND q.status='active' "
        "AND fixture.phase IN('scheduled','live') AND " + _matches('q', 'f.context') +
        "ORDER BY q.id LIMIT 8) x) options WHERE options.quote_ids IS NOT NULL "
        "AND NOT EXISTS(SELECT 1 FROM v2_expert_signals s JOIN v2_analysis_jobs j ON j.message_id=s.message_id "
        "WHERE j.id=$1 AND " + INTENT_VALID_SQL + " AND " + _matches('s', 'f.context') +
        "AND EXISTS(SELECT 1 FROM v2_analysis_traces t CROSS JOIN LATERAL "
        "jsonb_array_elements(t.payload->'accepted') a(value) WHERE t.job_id=$1 "
        "AND t.payload->>'attempt_id'=($2::uuid)::text AND t.trace_kind='mcp_record_expert_signals' "
        "AND t.payload->>'status'='succeeded' AND a.value->>'signal_id'=s.id::text)))"
    )


def intent_retry_projection():
    return (
        "(SELECT count(*) FROM intent_retries) AS line_intent_retry_count,"
        "(SELECT jsonb_agg(x) FROM (SELECT context AS intent_context,quote_ids AS matching_read_quote_ids "
        "FROM intent_retries ORDER BY context::text LIMIT $4) x) AS line_intent_retries"
    )
