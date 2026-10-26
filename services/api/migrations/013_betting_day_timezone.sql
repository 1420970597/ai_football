ALTER TABLE v2_user_bet_settings
 ADD COLUMN daily_timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai'
 CHECK(length(daily_timezone) BETWEEN 1 AND 100);

CREATE INDEX v2_order_open_exposure_idx ON v2_bet_orders(user_id)
 INCLUDE(reserved_stake) WHERE reserved_stake>0;
