-- 首页一键实盘：本人手动下注金额与既有日限额/敞口共用；manual 标记订单来源。
ALTER TABLE v2_user_bet_settings ADD COLUMN manual_stake NUMERIC(12,2) NOT NULL DEFAULT 0
    CHECK (manual_stake >= 0);
ALTER TABLE v2_bet_orders ADD COLUMN manual BOOLEAN NOT NULL DEFAULT FALSE;