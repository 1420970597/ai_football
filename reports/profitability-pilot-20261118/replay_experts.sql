-- Aggregate diagnostic of already executed automatic orders. No plan sends bets.
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;

WITH bounds AS (
    SELECT '2026-11-13 09:39:26+00'::timestamptz AS start_at,
           '2026-11-18 01:38:10.421809+00'::timestamptz AS end_at
), signals AS MATERIALIZED (
    SELECT s.*, f.sport,
      CASE WHEN s.phase='live' AND s.score_basis<>'full_period' THEN
        CASE s.period WHEN 'first_half' THEN jsonb_build_array(s.score_baseline->'home',s.score_baseline->'away',
              s.score_baseline->'half_home',s.score_baseline->'half_away')
          WHEN 'second_half' THEN s.score_baseline
          ELSE jsonb_build_array(s.score_baseline->'home',s.score_baseline->'away') END
        ELSE 'null'::jsonb END AS identity_baseline
    FROM v2_expert_signals s JOIN v2_fixtures f ON f.id=s.fixture_id
), candidates AS MATERIALIZED (
    SELECT o.id, o.created_at, o.provider_stake, o.settled_net, o.provider_price, o.provider_fees,
           o.state='settled' AND o.receipt_observed_at<b.end_at AS realized,
           s.expert_id, s.sport, s.fixture_id, s.phase, s.market_kind, s.period, s.score_basis
    FROM v2_bet_orders o JOIN signals s ON s.id=o.signal_id CROSS JOIN bounds b
    WHERE NOT o.manual AND o.created_at>=b.start_at AND o.created_at<b.end_at
      AND o.provider_placed_at<b.end_at AND o.provider_order_no IS NOT NULL
      AND s.sport='football'
), classified AS (
    SELECT c.*, e.effective, e.fixtures AS past_fixtures, e.net AS past_net
    FROM candidates c CROSS JOIN LATERAL (
       SELECT count(*) FILTER(WHERE h.outcome IN ('win','loss','half_win','half_loss')) AS effective,
              count(DISTINCT h.fixture_id) FILTER(WHERE h.outcome IN ('win','loss','half_win','half_loss')) AS fixtures,
              sum(h.unit_net) AS net
       FROM (
         SELECT s.fixture_id, l.outcome, l.unit_net, l.created_at,
                row_number() OVER (PARTITION BY s.fixture_id,s.market_id,s.market_kind,s.period,s.selection,
                    s.phase,s.score_basis,coalesce(s.line,0),s.identity_baseline ORDER BY s.created_at,s.id) AS intent_rank
         FROM signals s LEFT JOIN LATERAL (
           SELECT v.outcome,v.unit_net,v.created_at FROM v2_signal_settlements v
           JOIN v2_fixture_results r ON r.fixture_id=v.fixture_id AND r.version=v.result_version
           WHERE v.signal_id=s.id AND v.created_at<c.created_at AND r.received_at<c.created_at
             AND r.status IN ('confirmed','void')
           ORDER BY v.result_version DESC, v.created_at DESC LIMIT 1
         ) l ON true
         WHERE s.expert_id=c.expert_id AND s.sport=c.sport AND s.phase=c.phase AND s.fixture_id<>c.fixture_id
           AND s.created_at<c.created_at
           AND NOT (COALESCE(s.withdrawal_reason,'') IN ('duplicate_recommendation',
                'review_source_period_intent_ambiguous','review_source_period_intent_conflict',
                'review_source_line_intent_conflict','review_source_selection_intent_conflict',
                'review_source_period_not_supported','review_source_selection_not_supported')
                AND COALESCE(s.withdrawn_at<c.created_at,false))
       ) h WHERE h.intent_rank=1 AND h.created_at>=c.created_at-interval '30 days'
    ) e
), selected AS (
    SELECT rule, c.* FROM classified c CROSS JOIN LATERAL (VALUES
       ('all_received',true), ('prior_positive',c.effective>0 AND c.past_net>0),
       ('prior_positive_25_fixtures',c.past_fixtures>=25 AND c.past_net>0)
    ) p(rule, applies) WHERE p.applies
)
SELECT rule,phase,count(*) AS opportunities,count(*) FILTER(WHERE realized) AS settled,
       count(DISTINCT fixture_id) FILTER(WHERE realized) AS settled_fixtures,
       count(DISTINCT created_at::date) FILTER(WHERE realized) AS utc_dates,
       round(sum(settled_net / NULLIF(provider_stake,0)) FILTER(WHERE realized),4) AS flat_realized_units,
       round(avg(settled_net / NULLIF(provider_stake,0)) FILTER(WHERE realized)*100,2) AS flat_realized_pct,
       sum(settled_net) FILTER(WHERE realized) AS receipt_net,
       count(provider_fees) FILTER(WHERE realized) AS fees_present
FROM selected GROUP BY rule,phase ORDER BY phase,rule;

SELECT q.evidence_kind, count(*) AS records,
       count(*) FILTER(WHERE q.evidence ?| ARRAY['opposing_price','opposite_price','reference_book','quote_ladder']) AS reference_keys
FROM v2_quote_evidence q WHERE q.evidence_kind IN ('expert_signal','order_attempt')
GROUP BY q.evidence_kind ORDER BY q.evidence_kind;

ROLLBACK;
