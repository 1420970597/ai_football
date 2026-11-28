-- Audit native fills for overlapping contracts under the current order policy.
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH bounds AS (
    SELECT :'begin'::timestamptz AS starts_at, :'end'::timestamptz AS ends_at
), recent AS MATERIALIZED (
    SELECT o.id, s.fixture_id
    FROM v2_bet_orders o LEFT JOIN v2_expert_signals s ON s.id=o.signal_id
    CROSS JOIN bounds b
    WHERE o.provider_order_no IS NOT NULL
      AND coalesce(o.provider_placed_at,o.created_at) >= b.starts_at
      AND coalesce(o.provider_placed_at,o.created_at) < b.ends_at
      AND b.starts_at < b.ends_at AND b.ends_at <= now()
), fills AS MATERIALIZED (
    SELECT o.id, o.user_id, o.account_id, o.provider_identity_hash, o.provider_order_no, o.manual,
           o.provider_placed_at AS placed_at, s.fixture_id, f.sport, s.market_id, s.market_kind,
           s.period, s.phase, s.selection, s.line,
           CASE WHEN s.score_basis='unknown' AND s.phase='scheduled' THEN 'full_period'
                WHEN s.score_basis='unknown' AND s.phase='live'
                     AND f.sport='football' AND s.fixture_id ~ '^leyu:[0-9]+$'
                THEN CASE WHEN s.market_kind='total'
                               AND (s.market_id,s.period) IN (('2','full_time'),('18','first_half'))
                          THEN 'full_period'
                          WHEN s.market_kind='handicap' AND (s.market_id,s.period)=('4','full_time')
                          THEN 'remaining_period' ELSE s.score_basis END
                ELSE s.score_basis END AS basis,
           CASE WHEN jsonb_typeof(score.home)='number' AND jsonb_typeof(score.away)='number'
                     AND score.home::text ~ '^[0-9]+$' AND score.away::text ~ '^[0-9]+$'
                THEN jsonb_build_array(score.home,score.away) END AS baseline,
           num_nulls(o.user_id,o.account_id,o.provider_identity_hash,s.fixture_id,f.sport,s.market_id,
                     s.market_kind,s.period,s.phase,s.score_basis,s.selection)=0 AS has_contract
    FROM v2_bet_orders o LEFT JOIN v2_expert_signals s ON s.id=o.signal_id
    LEFT JOIN v2_fixtures f ON f.id=s.fixture_id
    CROSS JOIN bounds b
    CROSS JOIN LATERAL (
        SELECT coalesce(s.score_baseline->'home',s.score_baseline->'home_score') AS home,
               coalesce(s.score_baseline->'away',s.score_baseline->'away_score') AS away
    ) score
    WHERE o.provider_order_no IS NOT NULL
      AND coalesce(o.provider_placed_at,o.created_at) < b.ends_at
      AND (o.id IN (SELECT id FROM recent) OR s.fixture_id IN (SELECT fixture_id FROM recent))
), comparable_pairs AS (
    SELECT later.id, later.manual, later.placed_at, later.market_kind, later.sport,
           later.provider_order_no = earlier.provider_order_no AS same_receipt,
           later.line, earlier.line AS earlier_line, earlier.placed_at AS earlier_placement,
           earlier.id AS earlier_id, earlier.provider_order_no AS earlier_order_no,
           later.line IS NOT DISTINCT FROM earlier.line
               OR later.market_kind='winner' AND coalesce(later.line,0)=0 AND coalesce(earlier.line,0)=0 AS exact_line,
           later.basis='full_period'
               OR later.baseline IS NOT NULL AND later.baseline=earlier.baseline AS verified_scope
    FROM recent JOIN fills later USING (id)
    JOIN fills earlier ON earlier.id<>later.id AND earlier.has_contract
         AND (earlier.user_id,earlier.account_id,earlier.provider_identity_hash,earlier.fixture_id,
              earlier.market_id,earlier.market_kind,earlier.period,earlier.selection,earlier.basis)
           = (later.user_id,later.account_id,later.provider_identity_hash,later.fixture_id,
              later.market_id,later.market_kind,later.period,later.selection,later.basis)
         AND (later.basis='full_period' OR earlier.phase=later.phase
              AND (earlier.baseline IS NULL OR later.baseline IS NULL OR earlier.baseline=later.baseline))
    WHERE later.has_contract AND later.placed_at IS NOT NULL
      AND (earlier.placed_at <= later.placed_at OR earlier.placed_at IS NULL)
), pairs AS (
    SELECT p.*, cert.settled_before,
           p.earlier_placement < p.placed_at AS ordered,
           NOT p.manual AND p.market_kind IN ('total','handicap') AND p.verified_scope
               AND p.line::text NOT IN ('NaN','Infinity','-Infinity')
               AND p.earlier_line::text NOT IN ('NaN','Infinity','-Infinity')
               AND p.line<>p.earlier_line
               AND abs(p.line-p.earlier_line) <= CASE WHEN p.sport='football' THEN 0.5 ELSE 1 END AS adjacent
    FROM comparable_pairs p
    CROSS JOIN LATERAL (
        SELECT EXISTS (
            SELECT 1 FROM v2_order_receipt_evidence e
            WHERE e.order_id=p.earlier_id AND e.observed_at < p.placed_at
              AND e.payload->>'order_no'=p.earlier_order_no AND e.payload->>'state'='settled'
        ) AS settled_before
    ) cert
), compared AS (
    SELECT id,
           bool_or(ordered AND NOT same_receipt AND exact_line) AS exact,
           bool_or(ordered AND NOT same_receipt AND adjacent AND NOT settled_before) AS adjacent_open,
           bool_or(same_receipt AND (exact_line OR adjacent)) AS reused_receipt,
           bool_or(ordered IS NOT TRUE AND NOT same_receipt AND (exact_line OR adjacent)) AS uncertain_ordering
    FROM pairs GROUP BY id
)
SELECT jsonb_build_object(
    'native_fills', count(*) FILTER (WHERE f.placed_at IS NOT NULL),
    'automatic_fills', count(*) FILTER (WHERE f.placed_at IS NOT NULL AND NOT f.manual),
    'fixtures', count(DISTINCT f.fixture_id) FILTER (WHERE f.placed_at IS NOT NULL),
    'missing_placement', count(*) FILTER (WHERE f.placed_at IS NULL),
    'missing_contract', count(*) FILTER (WHERE NOT f.has_contract),
    'same_contract', count(*) FILTER (WHERE c.exact),
    'nearby_open_contract', count(*) FILTER (WHERE c.adjacent_open),
    'reused_receipt', count(*) FILTER (WHERE c.reused_receipt),
    'uncertain_ordering', count(*) FILTER (WHERE c.uncertain_ordering)
)
FROM recent JOIN fills f USING (id) LEFT JOIN compared c USING (id);
ROLLBACK;
