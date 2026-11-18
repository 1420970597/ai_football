SELECT jsonb_build_object(
    'sampled_at', statement_timestamp(),
    'wallet', coalesce((
        SELECT to_jsonb(observation)
        FROM public.v2_profitability_wallet_observation() AS observation
    ), '{"observed_at":null,"available_balance":null}'::jsonb)
);
