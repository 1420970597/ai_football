-- Wake review workers only after an eligible queue transaction commits.
-- Payloads intentionally contain no message or user data.
CREATE FUNCTION v2_notify_review_job_ready() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state IN ('queued', 'retry')
       AND (TG_OP = 'INSERT' OR OLD.state IS DISTINCT FROM NEW.state) THEN
        PERFORM pg_notify('v2_analysis_job_ready', '');
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER v2_analysis_job_ready_notify
    AFTER INSERT OR UPDATE OF state ON v2_analysis_jobs
    FOR EACH ROW EXECUTE FUNCTION v2_notify_review_job_ready();
