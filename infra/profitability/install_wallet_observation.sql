CREATE OR REPLACE FUNCTION public.v2_profitability_wallet_observation()
RETURNS TABLE(observed_at timestamptz, available_balance numeric)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
    WITH scope AS (
        SELECT min(account_id::text)::uuid AS account_id
        FROM public.v2_bet_orders
        WHERE account_id IS NOT NULL AND provider_identity_hash IS NOT NULL
        HAVING count(DISTINCT account_id) = 1 AND count(DISTINCT provider_identity_hash) = 1
    )
    SELECT account.balance_observed_at, account.available_balance
    FROM scope
    JOIN public.v2_leyu_accounts AS account ON account.id = scope.account_id
    WHERE account.health_state = 'healthy'
$function$;

REVOKE ALL ON FUNCTION public.v2_profitability_wallet_observation() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.v2_profitability_wallet_observation() TO v2_profitability_watch;
