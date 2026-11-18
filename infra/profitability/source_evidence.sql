-- Cross-contract source diagnostic. Labels are local to this snapshot.
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH recent_jobs AS (
    SELECT source_id, count(*) AS reviewed,
           count(*) FILTER (WHERE state = 'succeeded') AS review_succeeded,
           count(*) FILTER (WHERE state = 'unresolved') AS unresolved
    FROM v2_analysis_jobs WHERE created_at >= now() - interval '72 hours' GROUP BY 1
), recent_signals AS (
    SELECT m.source_id, count(*) AS signals, count(DISTINCT s.fixture_id) AS fixtures,
           count(*) FILTER (WHERE s.withdrawn_at IS NOT NULL) AS withdrawn,
           count(*) FILTER (WHERE s.outcome = 'pending') AS pending
    FROM v2_expert_signals s JOIN v2_expert_messages m ON m.id = s.message_id
    WHERE s.created_at >= now() - interval '72 hours' GROUP BY 1
), funds AS (
    SELECT m.source_id, count(*) AS platform_settled,
           count(DISTINCT s.fixture_id) AS settled_fixtures,
           count(DISTINCT o.provider_placed_at::date) AS fill_dates,
           count(*) FILTER (WHERE o.provider_fees IS NOT NULL AND o.provider_currency IS NOT NULL) AS cost_fields_present,
           sum(o.settled_net) AS provider_net,
           round(avg(o.settled_net / nullif(o.provider_stake, 0)) * 100, 2) AS flat_receipt_pct
    FROM v2_bet_orders o
    JOIN v2_expert_signals s ON s.id = o.signal_id
    JOIN v2_expert_messages m ON m.id = s.message_id
    WHERE o.state = 'settled' AND coalesce(o.settled_at, o.receipt_observed_at) >= now() - interval '7 days'
    GROUP BY 1
), mappings AS (
    SELECT se.source_id, count(*) FILTER (WHERE se.enabled AND e.enabled) AS enabled_experts
    FROM v2_source_experts se JOIN v2_experts e ON e.id = se.expert_id GROUP BY 1
)
SELECT jsonb_build_object(
    'observed_at', now(),
    'source_count', count(*),
    'enabled_count', count(*) FILTER (WHERE enabled),
    'no_recent_review', count(*) FILTER (WHERE enabled AND reviewed IS NULL),
    'no_recent_signal', count(*) FILTER (WHERE enabled AND signals IS NULL),
    'no_recent_settlement', count(*) FILTER (WHERE enabled AND platform_settled IS NULL),
    'sources', jsonb_agg(to_jsonb(rows) ORDER BY label)
) FROM (
    SELECT row_number() OVER (ORDER BY src.id) AS label, src.enabled,
           coalesce(m.enabled_experts, 0) AS enabled_experts,
           j.reviewed, j.review_succeeded, j.unresolved, s.signals, s.fixtures, s.withdrawn, s.pending,
           f.platform_settled, f.settled_fixtures, f.fill_dates, f.cost_fields_present, f.provider_net, f.flat_receipt_pct
    FROM v2_expert_sources src
    LEFT JOIN mappings m ON m.source_id = src.id
    LEFT JOIN recent_jobs j ON j.source_id = src.id
    LEFT JOIN recent_signals s ON s.source_id = src.id
    LEFT JOIN funds f ON f.source_id = src.id
) rows;
ROLLBACK;
