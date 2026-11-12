-- Explicit manual confirmations are separate orders. Automatic recommendations
-- still have at most one order per user/signal; request keys remain globally unique.
DROP INDEX v2_order_user_signal_unique;
CREATE UNIQUE INDEX v2_order_user_signal_unique ON v2_bet_orders(user_id,signal_id)
    WHERE account_id IS NOT NULL AND NOT manual;
