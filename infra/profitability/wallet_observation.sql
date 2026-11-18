WITH sampled AS (
    SELECT statement_timestamp() AS sampled_at
), wallet AS (
    SELECT observation.observed_at, observation.available_balance
    FROM public.v2_profitability_wallet_observation() AS observation
)
SELECT jsonb_build_object(
    'sampled_at', sampled.sampled_at,
    'wallet', coalesce(to_jsonb(wallet), '{"observed_at":null,"available_balance":null}'::jsonb),
    'wallet_age_seconds', CASE WHEN wallet.observed_at IS NOT NULL
        THEN round(extract(epoch FROM sampled.sampled_at - wallet.observed_at), 2) END,
    'wallet_is_fresh', coalesce(wallet.available_balance IS NOT NULL
        AND wallet.observed_at >= sampled.sampled_at - interval '10 minutes'
        AND wallet.observed_at <= sampled.sampled_at + interval '60 seconds', false)
)
FROM sampled LEFT JOIN wallet ON true;
