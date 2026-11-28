ALTER TABLE v2_user_bet_settings
    ADD COLUMN auto_sport_scope TEXT NOT NULL DEFAULT 'both'
    CHECK (auto_sport_scope IN ('both', 'football', 'basketball'));
