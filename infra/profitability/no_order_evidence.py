"""Summarize recent signals without automatic orders using deployed intent rules."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import sys

import asyncpg
from app.config import database_url


def utc(value):
    moment = datetime.fromisoformat(value)
    if moment.utcoffset() != timedelta(0):
        raise ValueError('UTC boundaries required')
    return moment


async def evidence(conn, begin, end, same_intent_sql):
    result = await conn.fetchval("""
        WITH bindings AS (
            SELECT DISTINCT account_id,provider_identity_hash
            FROM v2_bet_orders WHERE provider_order_no IS NOT NULL
        ), scoped AS (
            SELECT a.id,a.user_id,a.provider_identity_hash,p.auto_sport_scope
            FROM bindings b JOIN v2_leyu_accounts a ON (a.id,a.provider_identity_hash)=(b.account_id,b.provider_identity_hash)
            JOIN v2_user_bet_settings p ON p.user_id=a.user_id
            WHERE (SELECT count(*) FROM bindings)=1
        ), authorizations AS MATERIALIZED (
            SELECT al.id,al.created_at,al.details->>'auto_sport_scope' AS sport_scope
            FROM scoped a JOIN v2_audit_log al ON al.actor_id=a.user_id AND al.resource_id=a.id::text
            WHERE al.action='auto_betting_authorized' AND al.resource_type='leyu_account'
        ), reasons AS (
            SELECT s.id,s.message_id,s.created_at,m.source_id,m.posted_at,
                f.sport,s.phase,s.market_kind,s.period, CASE
                WHEN prior_order.created_at<s.created_at THEN 'prior_same_intent_order'
                WHEN prior_order.created_at IS NOT NULL THEN 'same_intent_reserved_within_source_window'
                WHEN s.withdrawn_at<=s.created_at THEN 'withdrawn_at_recommendation'
                WHEN m.posted_at < s.created_at - interval '5 minutes'
                    THEN 'source_window_expired_on_recommendation'
                WHEN policy.sport_scope IN ('football','basketball') AND policy.sport_scope<>f.sport
                    AND m.posted_at IS NOT NULL
                    AND NOT EXISTS (SELECT 1 FROM authorizations t WHERE t.created_at>=s.created_at
                                    AND t.created_at<=m.posted_at+interval '5 minutes')
                    AND (policy.sport_scope=a.auto_sport_scope OR EXISTS (SELECT 1 FROM authorizations t
                                    WHERE t.created_at>m.posted_at+interval '5 minutes'))
                    THEN 'outside_audited_sport_scope'
                WHEN f.sport='football' AND s.market_kind='team_total'
                    THEN 'football_team_total_outside_current_dispatch'
                ELSE 'unconfirmed' END AS evidence,
                CASE
                    WHEN prior_order.created_at IS NULL THEN 'no_same_intent_order'
                    WHEN prior_order.has_native_acceptance THEN 'native_acceptance_recorded'
                    WHEN prior_order.has_unresolved_order THEN 'unresolved_same_intent_order'
                    WHEN prior_order.all_stopped_without_receipt THEN 'stopped_without_native_receipt'
                    ELSE 'same_intent_order_state_unverified'
                END AS order_evidence
            FROM v2_expert_signals s JOIN v2_fixtures f ON f.id=s.fixture_id
            LEFT JOIN v2_expert_messages m ON m.id=s.message_id CROSS JOIN scoped a
            LEFT JOIN LATERAL (
                SELECT t.sport_scope FROM authorizations t WHERE t.created_at<s.created_at
                ORDER BY t.created_at DESC,t.id DESC LIMIT 1
            ) policy ON true
            LEFT JOIN LATERAL (
                SELECT min(o.created_at) AS created_at,
                       bool_or(o.state IN ('accepted','settled') AND
                               nullif(btrim(o.provider_order_no),'') IS NOT NULL) AS has_native_acceptance,
                       bool_or(o.state IN ('pending','submitting','unknown')) AS has_unresolved_order,
                       bool_and(o.state IN ('cancelled','rejected') AND
                                nullif(btrim(o.provider_order_no),'') IS NULL) AS all_stopped_without_receipt
                FROM v2_bet_orders o JOIN v2_expert_signals previous ON previous.id=o.signal_id
                WHERE (o.user_id,o.account_id,o.provider_identity_hash)=(a.user_id,a.id,a.provider_identity_hash)
                  AND """ + same_intent_sql('previous', 's') + """
                  AND (o.created_at<s.created_at OR (
                       previous.created_at<s.created_at AND o.provider_order_no IS NOT NULL
                       AND o.created_at<m.posted_at + interval '5 minutes'
                       AND o.created_at<s.created_at + interval '5 minutes'
                       AND m.posted_at<=s.created_at))
            ) prior_order ON true
            WHERE s.created_at >= $1 AND s.created_at < $2
              AND NOT EXISTS (
                  SELECT 1 FROM v2_bet_orders o WHERE o.signal_id=s.id AND NOT o.manual
                    AND (o.user_id,o.account_id,o.provider_identity_hash)=(a.user_id,a.id,a.provider_identity_hash)
              )
        ), summaries AS (
            SELECT sport,phase,market_kind,period,evidence,count(*) AS recommendations
            FROM reasons GROUP BY sport,phase,market_kind,period,evidence
        ), order_summaries AS (
            SELECT evidence,order_evidence,count(*) AS recommendations
            FROM reasons GROUP BY evidence,order_evidence
        ), expiry_timing AS (
            SELECT r.message_id, CASE
                WHEN received.received_at NOT BETWEEN r.posted_at AND r.created_at
                    OR received.received_at IS NULL THEN 'receipt_timing_unverified'
                WHEN received.received_at > r.posted_at+interval '5 minutes' THEN 'before_telegram_receipt'
                WHEN NOT (received.received_at<=j.created_at AND j.created_at<=timing.first_claim_at
                    AND timing.first_claim_at<=timing.final_claim_at AND timing.final_claim_at<=timing.request_at
                    AND timing.request_at<=r.created_at) IS TRUE THEN 'review_timing_unverified'
                WHEN j.created_at>r.posted_at+interval '5 minutes' THEN 'before_job_creation'
                WHEN timing.first_claim_at>r.posted_at+interval '5 minutes' THEN 'before_first_claim'
                WHEN timing.final_claim_at>r.posted_at+interval '5 minutes' THEN 'after_first_before_successful_claim'
                WHEN timing.request_at>r.posted_at+interval '5 minutes' THEN 'before_successful_request'
                ELSE 'during_successful_review' END AS stage,
                EXISTS (SELECT 1 FROM v2_analysis_traces t WHERE t.job_id=j.id
                    AND t.created_at>=timing.first_claim_at AND t.created_at<timing.final_claim_at
                    AND t.trace_kind='review_failure' AND t.payload->>'failure_category'='provider_transient'
                    AND t.payload->>'failure_stage'='model_review') AS prior_provider_failure
            FROM reasons r
            LEFT JOIN LATERAL (
                SELECT received_at FROM v2_telegram_events e
                WHERE e.source_id=r.source_id AND e.message_id=r.message_id AND e.event_kind IN ('message','edit')
                ORDER BY e.received_at,e.id LIMIT 1
            ) received ON true
            LEFT JOIN LATERAL (
                SELECT evidence->>'attempt_id' AS attempt_id,
                       CASE WHEN pg_input_is_valid(evidence->>'job_id','uuid')
                           THEN (evidence->>'job_id')::uuid END AS job_id
                FROM v2_quote_evidence WHERE evidence_kind='expert_signal' AND reference_id=r.id
                ORDER BY captured_at,id LIMIT 1
            ) snapshot ON true
            LEFT JOIN v2_analysis_jobs j ON j.id=snapshot.job_id AND j.message_id=r.message_id
            LEFT JOIN LATERAL (
                SELECT min(t.created_at) FILTER (WHERE t.trace_kind='attempt_claimed') AS first_claim_at,
                       min(t.created_at) FILTER (WHERE t.trace_kind='attempt_claimed'
                           AND t.payload->>'attempt_id'=snapshot.attempt_id) AS final_claim_at,
                       min(t.created_at) FILTER (WHERE t.trace_kind='hermes_request'
                           AND t.payload->>'attempt_id'=snapshot.attempt_id
                           AND t.payload->>'message_id'=r.message_id::text) AS request_at
                FROM v2_analysis_traces t WHERE t.job_id=j.id AND t.created_at<=r.created_at
                  AND t.trace_kind IN ('attempt_claimed','hermes_request')
            ) timing ON true
            WHERE r.evidence='source_window_expired_on_recommendation'
        ), expiry_summaries AS (
            SELECT stage,count(*) AS recommendations,count(DISTINCT message_id) AS message_versions,
                   count(*) FILTER (WHERE prior_provider_failure) AS with_prior_provider_failure
            FROM expiry_timing GROUP BY stage
        )
        SELECT jsonb_build_object('begin',$1::timestamptz,'end',$2::timestamptz,'observed_at',now(),
            'unique_account_available',EXISTS(SELECT 1 FROM scoped),
            'reasons',coalesce((SELECT jsonb_agg(to_jsonb(r) ORDER BY sport,phase,market_kind,period,evidence)
                                  FROM summaries r),'[]'::jsonb),
            'same_intent_order_evidence',coalesce((SELECT jsonb_agg(to_jsonb(r) ORDER BY evidence,order_evidence)
                                                   FROM order_summaries r),'[]'::jsonb),
            'expired_source_stages',coalesce((SELECT jsonb_agg(to_jsonb(r) ORDER BY stage)
                                              FROM expiry_summaries r),'[]'::jsonb))
    """, begin, end)
    return json.loads(result)


async def main():
    from app.signal_identity import same_intent_sql

    begin, end = map(utc, sys.argv[1:])
    if not begin < end <= datetime.now(timezone.utc) or end-begin > timedelta(hours=48):
        raise ValueError('completed range of at most 48 hours required')
    conn = await asyncpg.connect(database_url(), timeout=5, command_timeout=10)
    try:
        async with conn.transaction(readonly=True, isolation='repeatable_read'):
            print(json.dumps(await evidence(conn, begin, end, same_intent_sql)))
    finally:
        await conn.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        raise SystemExit(type(exc).__name__) from None
