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
            SELECT a.id,a.user_id,a.provider_identity_hash,p.auto_sport_scope,
                   (SELECT al.details->>'auto_sport_scope' FROM v2_audit_log al
                    WHERE al.action='auto_betting_authorized' AND al.resource_type='leyu_account'
                      AND al.actor_id=a.user_id AND al.resource_id=a.id::text AND al.created_at<$1
                    ORDER BY al.created_at DESC,al.id DESC LIMIT 1) AS previous_scope,
                   NOT EXISTS (
                       SELECT 1 FROM v2_audit_log al WHERE al.actor_id=a.user_id AND al.created_at>=$1
                         AND al.action IN ('auto_betting_authorized','automatic_betting_suspended',
                                           'emergency_stop','user_disabled')
                   ) AS no_later_policy_event
            FROM bindings b JOIN v2_leyu_accounts a ON (a.id,a.provider_identity_hash)=(b.account_id,b.provider_identity_hash)
            JOIN v2_user_bet_settings p ON p.user_id=a.user_id
            WHERE (SELECT count(*) FROM bindings)=1
        ), reasons AS (
            SELECT f.sport,s.phase,s.market_kind,s.period, CASE
                WHEN prior_order.created_at<s.created_at THEN 'prior_same_intent_order'
                WHEN prior_order.created_at IS NOT NULL THEN 'same_intent_reserved_within_source_window'
                WHEN a.no_later_policy_event AND a.previous_scope=a.auto_sport_scope
                    AND a.previous_scope IN ('football','basketball') AND a.previous_scope<>f.sport
                    THEN 'outside_audited_sport_scope'
                WHEN m.posted_at < s.created_at - interval '5 minutes'
                    THEN 'source_window_expired_on_recommendation'
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
        )
        SELECT jsonb_build_object('begin',$1::timestamptz,'end',$2::timestamptz,'observed_at',now(),
            'unique_account_available',EXISTS(SELECT 1 FROM scoped),
            'reasons',coalesce((SELECT jsonb_agg(to_jsonb(r) ORDER BY sport,phase,market_kind,period,evidence)
                                  FROM summaries r),'[]'::jsonb),
            'same_intent_order_evidence',coalesce((SELECT jsonb_agg(to_jsonb(r) ORDER BY evidence,order_evidence)
                                                   FROM order_summaries r),'[]'::jsonb))
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
