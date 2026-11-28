"""Measure archived market availability before an unresolved review ended."""

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json

import asyncpg
from app.config import database_url
from app.review_tool_evidence import replay_snapshot

ORDER_SOURCE_WINDOW = timedelta(minutes=5)

READS = """
WITH jobs AS MATERIALIZED (
    SELECT j.id, j.attempt_id, j.message_posted_at, j.created_at,
           jsonb_array_length(m.media_objects) > 0 AS has_media
    FROM v2_analysis_jobs j JOIN v2_expert_messages m ON m.id=j.message_id
    WHERE j.created_at >= $1 AND j.created_at < $2
      AND j.state='unresolved' AND j.review_mode='real_time'
), matched AS (
    SELECT j.*, t.created_at AS read_at, t.sequence_no, t.payload
    FROM jobs j JOIN v2_analysis_traces t ON t.job_id=j.id
      AND t.payload->>'attempt_id'=j.attempt_id::text
      AND t.trace_kind='mcp_list_match_markets' AND t.payload->>'status'='succeeded'
      AND t.payload->'market_coverage'->>'matched_quotes' ~ '^[1-9][0-9]{0,5}$'
    CROSS JOIN LATERAL (
        SELECT r.created_at, r.sequence_no, r.payload
        FROM v2_analysis_traces r WHERE r.job_id=j.id AND r.payload->>'attempt_id'=j.attempt_id::text
          AND r.trace_kind='system_review_result' ORDER BY r.sequence_no DESC LIMIT 1
    ) conclusion
    WHERE conclusion.payload->>'accepted_count'='0'
      AND conclusion.created_at < $2 AND t.sequence_no < conclusion.sequence_no
      AND NOT EXISTS (
          SELECT 1 FROM v2_analysis_traces w WHERE w.job_id=j.id
            AND w.payload->>'attempt_id'=j.attempt_id::text
            AND w.trace_kind='mcp_record_expert_signals'
      )
)
SELECT m.*, chunks.rows AS chunks
FROM matched m LEFT JOIN LATERAL (
    SELECT jsonb_agg(c.payload ORDER BY c.sequence_no) AS rows
    FROM v2_analysis_traces c WHERE c.job_id=m.id AND c.payload->>'attempt_id'=m.attempt_id::text
      AND c.trace_kind='tool_response_chunk' AND c.payload->>'tool'='mcp_list_match_markets'
      AND c.payload->>'call_id'=m.payload->>'call_id' AND c.sequence_no<m.sequence_no
) chunks ON true ORDER BY m.created_at,m.id,m.sequence_no
"""


def availability(payload, chunks):
    try:
        manifest = payload['response_snapshot']
        if (manifest['schema'], manifest['encoding'], manifest['call_id']) != (
                'review-tool-response-v1', 'base64-utf8-json', payload['call_id']):
            raise ValueError('unexpected tool response manifest')
        result = replay_snapshot(manifest, chunks)
        options = result['markets']
        coverage = result['market_coverage']
        if (not isinstance(options, list) or coverage.get('complete_for_filter') is not True
                or coverage.get('returned_quotes') != len(options)):
            raise ValueError('incomplete market response')
        quote_ids = [option['quote_id'] for option in options]
        if len(set(quote_ids)) != len(quote_ids) or sorted(quote_ids) != manifest['references']['quote_ids']:
            raise ValueError('market response references disagree')
        flags = [option.get('can_record') for option in options]
    except (KeyError, TypeError, ValueError, AttributeError):
        return 'snapshot_unavailable', Counter()
    if any(flag is True for flag in flags):
        return 'has_recordable_option', Counter()
    if not options or any(flag is not False for flag in flags):
        return 'option_availability_unknown', Counter()
    blockers = Counter()
    for option in options:
        if option.get('market_kind') == 'unsupported':
            blockers['unsupported'] += 1
        elif option.get('status') == 'active':
            blockers['active_without_usable_freshness'] += 1
        elif isinstance(option.get('status'), str) and option['status']:
            blockers['non_active'] += 1
        else:
            blockers['status_unknown'] += 1
    return 'no_recordable_option', blockers


def source_timing(message_posted_at, read_at):
    if message_posted_at is None or read_at < message_posted_at:
        return 'timing_unavailable'
    return ('within_order_source_window' if read_at - message_posted_at <= ORDER_SOURCE_WINDOW
            else 'older_than_order_source_window')


def summarize(rows):
    calls = Counter()
    blocked_options = Counter()
    per_job = defaultdict(set)
    media_by_job = {}
    for row in rows:
        state, blockers = availability(json.loads(row['payload']), json.loads(row['chunks'] or '[]'))
        timing = source_timing(row['message_posted_at'], row['read_at'])
        calls[timing, state] += 1
        if timing == 'within_order_source_window':
            blocked_options.update(blockers)
        per_job[row['id']].add((timing, state))
        media_by_job[row['id']] = row['has_media']
    jobs = Counter()
    for job_id, states in per_job.items():
        timely = {state for timing, state in states if timing == 'within_order_source_window'}
        if 'has_recordable_option' in timely:
            outcome = 'timely_recordable_option'
        elif timely == {'no_recordable_option'}:
            outcome = 'timely_no_recordable_option'
        elif timely:
            outcome = 'timely_evidence_gap'
        elif any(timing == 'timing_unavailable' for timing, _ in states):
            outcome = 'timing_unavailable'
        else:
            outcome = 'older_than_order_source_window'
        jobs[outcome, media_by_job[job_id]] += 1
    return {
        'jobs': [{'outcome': outcome, 'has_media': media, 'count': count}
                 for (outcome, media), count in sorted(jobs.items())],
        'calls': [{'timing': timing, 'availability': state, 'count': count}
                  for (timing, state), count in sorted(calls.items())],
        'timely_blocked_options': blocked_options,
    }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('begin', type=datetime.fromisoformat)
    parser.add_argument('end', type=datetime.fromisoformat)
    args = parser.parse_args()
    begin, end = args.begin, args.end
    if not (begin.utcoffset() == end.utcoffset() == timedelta(0)
            and timedelta(0) < end - begin <= timedelta(hours=72) and end <= datetime.now(timezone.utc)):
        raise ValueError('completed UTC interval of at most 72 hours required')
    conn = await asyncpg.connect(database_url(), timeout=5, command_timeout=15)
    try:
        async with conn.transaction(readonly=True, isolation='repeatable_read'):
            rows = await conn.fetch(READS, begin, end)
    finally:
        await conn.close()
    print(json.dumps({'begin': begin.isoformat(), 'end': end.isoformat(), **summarize(rows)}, sort_keys=True))


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as exc:
        raise SystemExit(type(exc).__name__) from None
