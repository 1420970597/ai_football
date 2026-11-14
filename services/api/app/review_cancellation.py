"""Release occupied model slots after their exact source attempt is revoked."""
import asyncio
import logging

import asyncpg

log = logging.getLogger('platform.review-worker')


async def cancel_ineligible_reviews(pool, active):
    """Read a bounded batch; uncertainty never cancels a valid review."""
    pending = {task: job for task, job in active.items() if not task.done()}
    if not pending:
        return
    try:
        async with asyncio.timeout(1):
            rows = await pool.fetch(
                "SELECT j.id,j.attempt_id,j.state,j.lease_until>clock_timestamp() AS leased,"
                "(m.deleted_at IS NULL AND m.media_state='ready' AND e.enabled AND s.enabled "
                "AND COALESCE(se.enabled,false) AND NOT EXISTS(SELECT 1 FROM v2_expert_messages newer "
                "WHERE newer.source_id=m.source_id AND newer.platform_message_id=m.platform_message_id "
                "AND newer.revision>m.revision)) AS eligible "
                "FROM v2_analysis_jobs j JOIN v2_expert_messages m ON m.id=j.message_id "
                "JOIN v2_experts e ON e.id=m.expert_id JOIN v2_expert_sources s ON s.id=m.source_id "
                "LEFT JOIN v2_source_experts se ON se.source_id=m.source_id AND se.expert_id=m.expert_id "
                "WHERE j.id=ANY($1::uuid[])",
                [job['id'] for job in pending.values()])
    except (TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError, ConnectionError, OSError) as exc:
        log.warning('review cancellation check deferred category=%s', type(exc).__name__)
        return
    current = {row['id']: row for row in rows}
    cancelled = []
    for task, job in pending.items():
        row = current.get(job['id'])
        if (row is None or row['attempt_id'] != job['attempt_id'] or not row['eligible']
                or row['state'] == 'failed' or row['state'] == 'running' and not row['leased']):
            task.cancel()
            cancelled.append(task)
    if cancelled:
        await asyncio.gather(*cancelled, return_exceptions=True)
        log.info('revoked review slots released count=%s', len(cancelled))
