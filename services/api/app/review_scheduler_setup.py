"""Transactional scheduler housekeeping with one remote database round trip."""


def persistent_retry_condition(job_alias: str, categories_parameter: str) -> str:
    return (
        f"{job_alias}.state IN('retry','failed') AND {job_alias}.review_mode='real_time' AND "
        "EXISTS(SELECT 1 FROM v2_analysis_traces t "
        f"WHERE t.job_id={job_alias}.id AND t.trace_kind='review_failure' "
        f"AND t.payload->>'attempt_id'={job_alias}.attempt_id::text "
        "AND t.payload->>'retryable'='true' "
        f"AND t.payload->>'failure_category'=ANY({categories_parameter}::text[]))"
    )


def claim_pair_and_job_query(lane, eligible_message, waiting_order, turn_ctes):
    """Lock one fair pair, skip locked jobs, and advance even an empty pair.

    The caller already holds the transaction-scoped scheduler lock. Activation
    and trace insertion stay in later statements so a prior trace committed
    while waiting on its advisory lock is visible to the sequence allocation.
    """
    persistent_retry = persistent_retry_condition('j', '$5')
    candidate = (
        "((j.state IN('queued','retry','running') AND j.attempts<$1) OR (" + persistent_retry + ")) AND " + lane +
        " AND ((j.state IN('queued','retry','failed') AND j.available_at<=statement_timestamp()) OR "
        "(j.state='running' AND (j.lease_until IS NULL OR j.lease_until<=statement_timestamp()))) AND " + eligible_message
    )
    return (
        "WITH selected_pair AS MATERIALIZED (SELECT ss.source_id,es.expert_id,es.concurrency_limit,"
        "(SELECT count(*) FROM v2_analysis_jobs running WHERE running.expert_id=es.expert_id "
        "AND running.state='running' AND running.lease_until>statement_timestamp()) AS active_leases "
        "FROM v2_review_source_schedule ss JOIN v2_expert_sources s ON s.id=ss.source_id "
        "JOIN v2_source_experts se ON se.source_id=s.id JOIN v2_experts e ON e.id=se.expert_id "
        "JOIN v2_review_expert_schedule es ON es.expert_id=e.id "
        "WHERE s.enabled AND e.enabled AND se.enabled AND (SELECT count(*) FROM v2_analysis_jobs running "
        "WHERE running.expert_id=es.expert_id AND running.state='running' AND running.lease_until>statement_timestamp())<GREATEST(es.concurrency_limit,$4) "
        "AND EXISTS(SELECT 1 FROM v2_analysis_jobs j JOIN v2_expert_messages m ON m.id=j.message_id "
        "WHERE j.source_id=ss.source_id AND j.expert_id=es.expert_id AND " + candidate + " ) "
        "ORDER BY ss.last_turn,es.last_turn,ss.source_id,es.expert_id LIMIT 1 "
        "FOR SHARE OF s,e,se FOR UPDATE OF ss,es), "
        "selected_job AS MATERIALIZED (SELECT j.id,j.message_id,j.attempts "
        "FROM v2_analysis_jobs j JOIN v2_expert_messages m ON m.id=j.message_id "
        "JOIN selected_pair p ON p.source_id=j.source_id AND p.expert_id=j.expert_id "
        "WHERE " + candidate +
        " ORDER BY (j.state='running') DESC,"
        "CASE WHEN j.state='running' THEN j.lease_until END NULLS FIRST,"
        "CASE WHEN j.state='running' THEN j.created_at END," + waiting_order +
        "CASE WHEN j.state<>'running' THEN j.message_posted_at END DESC,"
        "CASE WHEN j.state<>'running' THEN j.available_at END,j.created_at,j.id "
        "LIMIT 1 FOR UPDATE OF j SKIP LOCKED), " + turn_ctes +
        " SELECT p.source_id,p.expert_id,p.concurrency_limit,p.active_leases,"
        "j.id,j.message_id,j.attempts,(SELECT last_turn FROM advanced_expert) AS scheduler_turn "
        "FROM selected_pair p LEFT JOIN selected_job j ON true"
    )


async def prepare_claim_schedule(conn, max_attempts, batch_size, persistent_failures, *, scheduler_lock=None):
    """Use the caller's scheduler lock, or acquire it before any housekeeping.

    The scalar count dependencies force housekeeping before source/expert
    initialization, then the lane lock. Later candidate selection remains a
    separate statement so it sees the rows created by these writing CTEs.
    """
    # Materialization makes the non-blocking lock and transaction budgets a
    # prerequisite for every write, including empty-queue initialization.
    gate = (
        "scheduler_gate AS MATERIALIZED (SELECT set_config('lock_timeout','2000',true),"
        "set_config('statement_timeout','3000',true),pg_try_advisory_xact_lock(hashtext($4)) AS locked), "
        if scheduler_lock is not None else ''
    )
    allowed = '(SELECT locked FROM scheduler_gate) AND ' if scheduler_lock is not None else ''
    parameters = [max_attempts, batch_size, sorted(persistent_failures)]
    if scheduler_lock is not None:
        parameters.append(scheduler_lock)
    return await conn.fetchval(
        "WITH " + gate + "exhausted_candidates AS MATERIALIZED (SELECT j.id,j.state FROM v2_analysis_jobs j WHERE "
        + allowed + "((j.state='running' AND (j.lease_until IS NULL OR j.lease_until<=statement_timestamp()) AND j.attempts >= $1) "
        "OR (j.state='retry' AND j.attempts >= $1 AND NOT (" +
        persistent_retry_condition('j', '$3') + ")) "
        " ) ORDER BY j.lease_until NULLS FIRST,j.id LIMIT $2 FOR UPDATE SKIP LOCKED), "
        "exhausted AS (UPDATE v2_analysis_jobs j SET state='failed',lease_until=NULL,last_error=CASE "
        "WHEN x.state='running' THEN 'lease_exhausted' ELSE 'retry_limit_without_transient_trace' END,updated_at=now() "
        "FROM exhausted_candidates x WHERE j.id=x.id RETURNING j.id), "
        "initialized_sources AS (INSERT INTO v2_review_source_schedule(source_id) "
        "SELECT id FROM v2_expert_sources WHERE " + allowed + "(SELECT count(*) FROM exhausted)>=0 "
        "ON CONFLICT DO NOTHING RETURNING source_id), "
        "initialized_experts AS (INSERT INTO v2_review_expert_schedule(expert_id) "
        "SELECT id FROM v2_experts WHERE " + allowed + "(SELECT count(*) FROM initialized_sources)>=0 "
        "ON CONFLICT DO NOTHING RETURNING expert_id) "
        "SELECT realtime_claims_since_replay FROM v2_review_lane_schedule "
        "WHERE " + allowed + "singleton AND (SELECT count(*) FROM initialized_experts)>=0 FOR UPDATE",
        *parameters,
        # SET LOCAL applies to subsequent statements. Bound this combined
        # statement itself as well, instead of inheriting the pool's 5s limit.
        **({'timeout': 3.0} if scheduler_lock is not None else {}),
    )
