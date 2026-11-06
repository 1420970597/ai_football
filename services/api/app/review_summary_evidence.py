"""One consistent, attempt-scoped snapshot for the deterministic receipt."""
from dataclasses import dataclass
from datetime import datetime

from .review_completion import DETAIL_LIMIT, pagination_issues, unfinished_searches_cte
from .review_period_intent import INTENT_VALID_SQL


@dataclass(frozen=True)
class SummaryEvidence:
    total: int
    signals: list[dict]
    rejected: list[dict]
    identified: list[dict]
    model_contents: list[str]
    pagination_issues: dict


def _timestamps(rows: list[dict]) -> list[dict]:
    # PostgreSQL JSON timestamps trim trailing microseconds. Reconstruct the
    # datetime so the existing receipt formatter keeps its exact ISO contract.
    for row in rows:
        for key in ('kickoff_at', 'quote_time'):
            if isinstance(row.get(key), str):
                row[key] = datetime.fromisoformat(row[key])
    return rows


async def read_summary_evidence(conn, job_id, attempt_id, message_id, limit: int) -> SummaryEvidence:
    row = await conn.fetchrow(
        "/* v2-summary-evidence */ WITH " + unfinished_searches_cte() + ", message_signals AS MATERIALIZED ("
        " SELECT id,fixture_id,phase,market_id,market_name,market_kind,period,line,selection,"
        " recommendation_price,quote_time,created_at FROM v2_expert_signals s WHERE message_id=$3 AND " + INTENT_VALID_SQL + "),"
        " attempt_traces AS MATERIALIZED ("
        " SELECT trace_kind,payload,sequence_no FROM v2_analysis_traces WHERE job_id=$1 "
        " AND payload->>'attempt_id'=($2::uuid)::text "
        " AND trace_kind IN('mcp_record_expert_signals','hermes_result','hermes_partial_result')) "
        "SELECT (SELECT count(*) FROM unfinished) AS unfinished_count,"
        " (SELECT jsonb_agg(x) FROM (SELECT * FROM unfinished ORDER BY search_key LIMIT $5) x) AS unfinished,"
        " (SELECT count(*) FROM message_signals) AS total,"
        " (SELECT jsonb_agg(to_jsonb(x)-'created_at' ORDER BY x.created_at,x.signal_id) FROM ("
        " SELECT s.id AS signal_id,s.fixture_id,f.home_team,f.away_team,f.competition_name,f.kickoff_at,"
        " s.phase,s.market_id,s.market_name,s.market_kind,s.period,s.line::text AS line,s.selection,"
        " s.recommendation_price::text AS recommendation_price,s.quote_time,s.created_at "
        " FROM message_signals s JOIN v2_fixtures f ON f.id=s.fixture_id "
        " ORDER BY s.created_at,s.id LIMIT $4) x) AS signals,"
        " (SELECT jsonb_agg(x.rejection ORDER BY x.sequence_no,x.position) FROM ("
        " SELECT t.sequence_no,r.position,r.rejection,CASE "
        " WHEN r.rejection->>'index' ~ '^[0-9]{1,2}$' THEN "
        " t.payload->'parameters'->'signals'->((r.rejection->>'index')::int) END AS pick "
        " FROM attempt_traces t CROSS JOIN LATERAL "
        " jsonb_array_elements(t.payload->'rejected') WITH ORDINALITY r(rejection,position) "
        " WHERE t.trace_kind='mcp_record_expert_signals') x "
        " WHERE NOT EXISTS(SELECT 1 FROM message_signals s "
        " JOIN v2_quote_evidence e ON e.reference_id=s.id AND e.evidence_kind='expert_signal' "
        " AND e.fixture_id=s.fixture_id "
        " WHERE s.fixture_id=x.pick->>'fixture_id' AND e.evidence->>'quote_id'=x.pick->>'quote_id')) AS rejected,"
        " (SELECT jsonb_agg(to_jsonb(x) ORDER BY x.kickoff_at,x.fixture_id) FROM ("
        " SELECT b.fixture_id,f.home_team,f.away_team,f.competition_name,f.kickoff_at,f.phase,count(*) OVER() AS total "
        " FROM v2_review_fixture_bindings b JOIN v2_fixtures f ON f.id=b.fixture_id "
        " WHERE b.job_id=$1 AND b.attempt_id=$2 AND NOT EXISTS("
        " SELECT 1 FROM message_signals s WHERE s.fixture_id=b.fixture_id) "
        " ORDER BY f.kickoff_at,b.fixture_id LIMIT $4) x) AS identified,"
        " (SELECT jsonb_agg(payload->>'content' ORDER BY sequence_no) FROM attempt_traces "
        " WHERE trace_kind IN('hermes_result','hermes_partial_result')) AS model_contents",
        job_id, attempt_id, message_id, limit, DETAIL_LIMIT,
    )
    return SummaryEvidence(
        total=row['total'], signals=_timestamps(row['signals'] or []),
        rejected=row['rejected'] or [],
        identified=_timestamps(row['identified'] or []),
        model_contents=[content or '' for content in (row['model_contents'] or [])],
        pagination_issues=pagination_issues(row['unfinished_count'], row['unfinished']),
    )
