CREATE INDEX v2_fixtures_latest_source_idx ON v2_fixtures(source_updated_at DESC NULLS LAST) WHERE phase IN('live','scheduled');
CREATE INDEX v2_quotes_latest_source_idx ON v2_current_quotes(source_updated_at DESC NULLS LAST) WHERE status='active';
CREATE INDEX v2_analysis_backlog_time_idx ON v2_analysis_jobs(created_at) WHERE state IN('queued','retry','running');
CREATE INDEX v2_messages_pending_media_idx ON v2_expert_messages(created_at) WHERE media_state<>'ready' AND deleted_at IS NULL;
CREATE INDEX v2_messages_received_time_idx ON v2_expert_messages(created_at DESC);
CREATE INDEX v2_orders_admin_time_idx ON v2_bet_orders(created_at DESC,id DESC);
CREATE INDEX v2_orders_state_time_idx ON v2_bet_orders(state,created_at DESC,id DESC);
CREATE INDEX v2_audit_admin_time_idx ON v2_audit_log(created_at DESC,id DESC);
