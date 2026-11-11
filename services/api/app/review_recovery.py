"""Bounded recovery from tool-confirmed transient quote refresh failures."""
from __future__ import annotations

from datetime import datetime, timezone

from .review_policy import REALTIME_WINDOW_SECONDS

# One additional model attempt; do not turn unavailable markets into an
# unbounded refresh loop or compete indefinitely with new Telegram messages.
MARKET_RECOVERY_ATTEMPTS = 2
TRANSIENT_REFRESH_REASONS = (
    'refresh_timeout', 'refresh_unavailable', 'refresh_busy',
    'refresh_throttled', 'ingestion_failed', 'refresh_failed', 'upstream_timeout',
)
# The collector refused or lost the provider call under its shared backoff.
# Every quote of the bound fixture then stays unconfirmed, so an explicit
# recommendation would otherwise end unresolved without any later check.
# Wait out the collector's own window instead of probing inside it.
UPSTREAM_BACKOFF_REASONS = ('upstream_cooldown', 'upstream_rate_limited')
RECOVERY_DELAY_SECONDS = 20
UPSTREAM_BACKOFF_MIN_SECONDS = 60
UPSTREAM_BACKOFF_MAX_SECONDS = 900
UPSTREAM_BACKOFF_MARGIN_SECONDS = 5


def recovery_delay_seconds(rows) -> int:
    """Longest collector backoff among the pending fixtures, bounded to 15 minutes."""
    delay = RECOVERY_DELAY_SECONDS
    for row in rows:
        if row['reason'] in UPSTREAM_BACKOFF_REASONS:
            hint = row['retry_after'] if type(row['retry_after']) is int else 0
            delay = max(delay, UPSTREAM_BACKOFF_MARGIN_SECONDS + min(
                UPSTREAM_BACKOFF_MAX_SECONDS, max(UPSTREAM_BACKOFF_MIN_SECONDS, hint)))
    return delay


async def market_recovery(conn, job, attempt_id, attempts: int) -> dict | None:
    """Caller has validated and locked the current eligible message attempt.

    Use system tool receipts, never model prose. A later successful read wins
    over an earlier failure; accepted market identities remain immutable.
    """
    age = (datetime.now(timezone.utc) - job['posted_at']).total_seconds()
    if (job['review_mode'] != 'real_time' or not 0 <= age <= REALTIME_WINDOW_SECONDS
            or attempts >= MARKET_RECOVERY_ATTEMPTS):
        return None
    rows = await conn.fetch(
        "WITH latest AS MATERIALIZED ("
        " SELECT DISTINCT ON (payload->>'fixture_id') payload FROM v2_analysis_traces "
        " WHERE job_id=$1 AND trace_kind='mcp_list_match_markets' "
        " AND payload->>'attempt_id'=$2 AND payload ? 'fixture_id' "
        " ORDER BY payload->>'fixture_id',sequence_no DESC), candidates AS ("
        " SELECT payload->>'fixture_id' AS fixture_id,payload->'refresh'->>'reason' AS reason,NULL::uuid AS quote_id,"
        " CASE WHEN jsonb_typeof(payload->'refresh'->'retry_after_seconds')='number' "
        " AND payload->'refresh'->>'retry_after_seconds' ~ '^[0-9]{1,3}$' "
        " THEN (payload->'refresh'->>'retry_after_seconds')::int END AS retry_after FROM latest "
        " WHERE payload->>'status'='succeeded' AND payload->'refresh'->>'attempted'='true' "
        " AND payload->'refresh'->>'reason'=ANY($4::text[]) UNION "
        " SELECT q.fixture_id,'selected_quote_temporarily_unavailable',q.id,NULL::int FROM v2_analysis_traces t "
        " CROSS JOIN LATERAL jsonb_array_elements(t.payload->'parameters'->'signals') WITH ORDINALITY AS p(pick,position) "
        " CROSS JOIN LATERAL jsonb_array_elements(t.payload->'rejected') AS r(rejection) "
        " JOIN v2_current_quotes q ON q.id=CASE "
        " WHEN (p.pick->>'quote_id') ~* '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' "
        " THEN (p.pick->>'quote_id')::uuid END "
        " AND q.fixture_id=p.pick->>'fixture_id' AND q.status='active' "
        " WHERE t.job_id=$1 AND t.trace_kind='mcp_record_expert_signals' "
        " AND t.payload->>'attempt_id'=$2 AND t.payload->>'status'='succeeded' "
        " AND r.rejection->>'index'=(p.position-1)::text "
        " AND r.rejection->>'reason'='quote_inactive_unknown_or_stale') "
        "SELECT DISTINCT ON(b.fixture_id) b.fixture_id,c.reason,c.retry_after FROM candidates c "
        "JOIN v2_review_fixture_bindings b ON b.fixture_id=c.fixture_id "
        "AND b.job_id=$1 AND b.attempt_id=$3 "
        "JOIN v2_fixtures f ON f.id=b.fixture_id AND f.phase IN('scheduled','live') "
        "LEFT JOIN v2_current_quotes selected ON selected.id=c.quote_id "
        "WHERE NOT EXISTS(SELECT 1 FROM v2_expert_signals s "
        "WHERE s.message_id=$5 AND s.fixture_id=b.fixture_id AND (c.quote_id IS NULL OR ("
        "s.market_id=selected.market_id AND s.period=selected.period "
        "AND s.line IS NOT DISTINCT FROM selected.line AND s.selection=selected.selection))) "
        "ORDER BY b.fixture_id,c.reason",
        job['id'], str(attempt_id), attempt_id,
        list(TRANSIENT_REFRESH_REASONS + UPSTREAM_BACKOFF_REASONS), job['message_id'],
    )
    if not rows:
        return None
    return {'reason': 'temporary_market_refresh_failure', 'delay_seconds': recovery_delay_seconds(rows),
            'attempt_limit': MARKET_RECOVERY_ATTEMPTS,
            'fixtures': [{'fixture_id': row['fixture_id'], 'reason': row['reason'],
                          **({'retry_after_seconds': row['retry_after']} if row['retry_after'] is not None else {})}
                         for row in rows]}
