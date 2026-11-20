CREATE INDEX v2_telegram_event_members_by_message_idx
    ON v2_telegram_event_members(platform_message_id, event_id);
