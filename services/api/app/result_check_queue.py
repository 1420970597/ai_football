"""Bounded first-result priority with capacity for official result corrections."""
from __future__ import annotations

import uuid


async def claim_due_results(conn, limit: int):
    # Rank within each lane before locking. The final selection locks real check
    # rows, skips competing owners and rechecks due/lease predicates. Extra
    # first-result work fills spare correction capacity without more requests.
    return await conn.fetch(
        """WITH due AS MATERIALIZED (
            SELECT c.fixture_id,c.next_check_at,f.source_id,f.kickoff_at,
                   f.home_team,f.away_team,f.sport,
                   COALESCE(r.status IN ('confirmed','void'),false) AS has_final
            FROM v2_result_checks c JOIN v2_fixtures f ON f.id=c.fixture_id
            LEFT JOIN LATERAL (
                SELECT status,received_at FROM v2_fixture_results
                WHERE fixture_id=f.id ORDER BY version DESC LIMIT 1
            ) r ON true
            WHERE c.next_check_at<=now()
              AND (c.lease_until IS NULL OR c.lease_until<now())
              AND f.kickoff_at>now()-interval '30 days'
              AND (f.phase='finished' OR f.kickoff_at<=now()-interval '100 minutes')
              AND NOT COALESCE(r.status='confirmed'
                  AND r.received_at<now()-interval '7 days',false)
        ), ranked AS (
            SELECT *,row_number() OVER (
                PARTITION BY has_final ORDER BY next_check_at,fixture_id
            ) AS lane_position FROM due
        ), scheduled AS (
            SELECT *,CASE
                WHEN NOT has_final AND lane_position<=GREATEST(1,$1::int-1) THEN 0
                WHEN has_final THEN 1 ELSE 2 END AS priority FROM ranked
        ), chosen AS MATERIALIZED (
            SELECT s.* FROM scheduled s
            JOIN v2_result_checks c ON c.fixture_id=s.fixture_id
            WHERE c.next_check_at<=now()
              AND (c.lease_until IS NULL OR c.lease_until<now())
            ORDER BY s.priority,s.next_check_at,s.fixture_id
            LIMIT $1 FOR UPDATE OF c SKIP LOCKED
        ), numbered AS (
            SELECT *,row_number() OVER (
                ORDER BY priority,next_check_at,fixture_id
            )::int AS lease_position FROM chosen
        ), leased AS (
            UPDATE v2_result_checks c
            SET lease_id=($2::uuid[])[n.lease_position],
                lease_until=now()+interval '90 seconds'
            FROM numbered n WHERE c.fixture_id=n.fixture_id
            RETURNING c.fixture_id,n.source_id,n.kickoff_at,n.home_team,
                      n.away_team,n.sport,c.lease_id,n.lease_position
        ) SELECT * FROM leased ORDER BY lease_position""",
        limit, [uuid.uuid4() for _ in range(limit)],
    )
