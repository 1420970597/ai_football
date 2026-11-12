-- New observations only: do not invent detection times for existing messages.
ALTER TABLE v2_expert_messages ADD COLUMN IF NOT EXISTS detected_at TIMESTAMPTZ;
COMMENT ON COLUMN v2_expert_messages.detected_at IS
    'Collector first detection of this message version; immutable across media completion/retry. NULL means legacy timing unknown.';
