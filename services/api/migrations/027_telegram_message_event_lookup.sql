-- Read the latest source event for one immutable message without a table scan.
CREATE INDEX IF NOT EXISTS v2_telegram_events_message_latest_idx
    ON v2_telegram_events(message_id,received_at DESC,id DESC);

-- Accept a concurrently prepared production index only when it is exact.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_index
        WHERE indexrelid = 'public.v2_telegram_events_message_latest_idx'::regclass
          AND indisvalid AND indisready
          AND pg_get_indexdef(indexrelid) =
            'CREATE INDEX v2_telegram_events_message_latest_idx ON public.v2_telegram_events USING btree (message_id, received_at DESC, id DESC)'
    ) THEN
        RAISE EXCEPTION 'message event index definition or validity mismatch';
    END IF;
END $$;
