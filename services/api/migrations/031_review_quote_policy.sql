-- Only a non-secret threshold is projected; the restricted role cannot read
-- the encrypted administrator settings or write this projection.
CREATE TABLE v2_review_quote_policy (
    setting_key TEXT PRIMARY KEY CHECK (setting_key='quote_max_age_seconds'),
    max_age_seconds INTEGER NOT NULL CHECK (max_age_seconds BETWEEN 1 AND 3600),
    source_cipher_sha256 TEXT NOT NULL CHECK (source_cipher_sha256 ~ '^[0-9a-f]{64}$'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE SCHEMA v2_mcp;
REVOKE ALL ON SCHEMA v2_mcp FROM PUBLIC;
CREATE VIEW v2_mcp.v2_admin_settings WITH (security_barrier=true) AS
SELECT s.setting_key,
    CASE WHEN p.source_cipher_sha256=encode(sha256(convert_to(s.encrypted_value,'UTF8')),'hex')
         THEN 'mcp-quote-policy-v1:' || p.max_age_seconds::text
         ELSE 'mcp-quote-policy-unverified' END AS encrypted_value,
    NULL::uuid AS updated_by,s.updated_at
FROM public.v2_admin_settings s
LEFT JOIN public.v2_review_quote_policy p ON p.setting_key=s.setting_key
WHERE s.setting_key='quote_max_age_seconds';
REVOKE ALL ON v2_mcp.v2_admin_settings FROM PUBLIC;
