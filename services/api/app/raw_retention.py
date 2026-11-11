"""Bounded raw-data TTL, independent of recommendation or outcome calculations."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import asyncpg

log = logging.getLogger('platform.raw-retention')
LOCK = 'platform-v2-local-raw-retention'


def validate_local_target(dsn: str):
    parts = urlsplit(dsn)
    query = parse_qs(parts.query)
    if (parts.hostname != 'local-postgres' or parts.port not in (None, 5432)
            or parts.path != '/football_v2' or parts.fragment
            or set(query) - {'sslmode', 'sslrootcert'}
            or query.get('sslmode') != ['verify-full']):
        raise ValueError('retention_requires_verified_local_database')


def _name(value):
    if not re.fullmatch(r'v2_[a-z0-9_]+', value):
        raise ValueError('unexpected_retention_table')
    return '"' + value + '"'


async def unreferenced(conn, parent, *, cascades=False, correlated=False):
    """Use actual incoming keys; do not bypass an unknown future constraint."""
    rows = await conn.fetch(
        "SELECT c.relname AS name,n.nspname AS schema,f.confdeltype,"
        "array_agg(a.attname ORDER BY k.ord) AS child_columns,"
        "array_agg(b.attname ORDER BY k.ord) AS parent_columns "
        "FROM pg_constraint f JOIN pg_class c ON c.oid=f.conrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "CROSS JOIN LATERAL unnest(f.conkey,f.confkey) WITH ORDINALITY k(child,parent,ord) "
        "JOIN pg_attribute a ON a.attrelid=f.conrelid AND a.attnum=k.child "
        "JOIN pg_attribute b ON b.attrelid=f.confrelid AND b.attnum=k.parent "
        "WHERE f.contype='f' AND f.confrelid=$1::regclass "
        "GROUP BY f.oid,c.relname,n.nspname,f.confdeltype", parent)
    clauses = []
    for row in rows:
        if cascades and row['confdeltype'] in ('c', b'c'):
            continue
        if row['schema'] != 'public':
            raise ValueError('external_retention_reference')
        pairs = []
        for child, key in zip(row['child_columns'], row['parent_columns']):
            if not all(re.fullmatch(r'[a-z_][a-z0-9_]*', v) for v in (child, key)):
                raise ValueError('unexpected_retention_column')
            pairs.append('r."' + child + '"=p."' + key + '"')
        # On the very large current-quote child, a hash anti join can scan
        # millions of rows before honoring the parent batch limit. All
        # fixture reference columns are indexed by the retention migration.
        tail = ' OFFSET 0' if correlated else ''
        clauses.append('NOT EXISTS(SELECT 1 FROM ' + _name(row['name']) + ' r WHERE ' + ' AND '.join(pairs) + tail + ')')
    return ' AND '.join(clauses) or 'TRUE'


async def sweep(conn, *, limit=1000, quote_limit=5000):
    if not 1 <= limit <= 5000 or not 1 <= quote_limit <= 20000:
        raise ValueError('invalid_retention_batch_budget')
    counts = {}
    async with conn.transaction():
        await conn.execute("SET LOCAL statement_timeout='5000'")
        await conn.execute("SET LOCAL lock_timeout='1000'")
        if not await conn.fetchval('SELECT pg_try_advisory_xact_lock(hashtext($1))', LOCK):
            return {'skipped_locked': 1}
        await conn.execute("UPDATE v2_raw_retention_status SET last_started_at=clock_timestamp() WHERE singleton")

        async def run(name, sql, batch=limit):
            try:
                result = await conn.execute(sql, batch)
            except Exception as exc:
                log.error('retention_statement_failed operation=%s category=%s', name, type(exc).__name__)
                raise
            counts[name] = int(result.rsplit(' ', 1)[-1])

        await run('messages_expired',
            "WITH old AS (SELECT id FROM v2_expert_messages WHERE raw_content_expired_at IS NULL "
            "AND posted_at<clock_timestamp()-interval '72 hours' "
            "ORDER BY posted_at,id LIMIT $1 FOR UPDATE SKIP LOCKED) "
            "UPDATE v2_expert_messages m SET raw_content_expired_at=clock_timestamp() FROM old WHERE m.id=old.id")
        await run('events_expired',
            "WITH old AS (SELECT id FROM v2_telegram_events WHERE raw_content_expired_at IS NULL "
            "AND COALESCE(message_posted_at,(content->>'posted_at')::timestamptz,source_version_at)<clock_timestamp()-interval '72 hours' "
            "ORDER BY source_version_at,id LIMIT $1 FOR UPDATE SKIP LOCKED) "
            "UPDATE v2_telegram_events e SET raw_content_expired_at=clock_timestamp() FROM old WHERE e.id=old.id")
        await run('jobs_cancelled',
            "WITH old AS (SELECT j.id FROM v2_analysis_jobs j JOIN v2_expert_messages m ON m.id=j.message_id "
            "WHERE m.raw_content_expired_at IS NOT NULL AND j.state IN('queued','running','retry') "
            "LIMIT $1 FOR UPDATE OF j SKIP LOCKED) UPDATE v2_analysis_jobs j "
            "SET state='failed',lease_until=NULL,last_error='raw_content_expired' FROM old WHERE j.id=old.id")
        await run('traces_deleted',
            "WITH old AS (SELECT t.id FROM v2_analysis_traces t JOIN v2_analysis_jobs j ON j.id=t.job_id "
            "JOIN v2_expert_messages m ON m.id=j.message_id WHERE m.raw_content_expired_at IS NOT NULL "
            "LIMIT $1 FOR UPDATE OF t SKIP LOCKED) DELETE FROM v2_analysis_traces t USING old WHERE t.id=old.id", quote_limit)
        # These are temporary search/market records, not immutable accepted
        # quote evidence or the permanently stored parsed decision.
        for table in ('v2_review_market_reads', 'v2_review_fixture_bindings', 'v2_match_searches'):
            expired = 'm.raw_content_expired_at IS NOT NULL'
            if table == 'v2_review_market_reads':
                expired += (" OR EXISTS(SELECT 1 FROM v2_current_quotes q JOIN v2_fixtures f ON f.id=q.fixture_id "
                            "WHERE q.id=p.quote_id AND f.kickoff_at<clock_timestamp()-interval '72 hours')")
            elif table == 'v2_review_fixture_bindings':
                expired += (" OR EXISTS(SELECT 1 FROM v2_fixtures f WHERE f.id=p.fixture_id "
                            "AND f.kickoff_at<clock_timestamp()-interval '72 hours')")
            await run(table + '_deleted',
                'WITH old AS (SELECT p.ctid AS tid FROM ' + _name(table) + ' p '
                'JOIN v2_analysis_jobs j ON j.id=p.job_id '
                'JOIN v2_expert_messages m ON m.id=j.message_id '
                'WHERE (' + expired + ') LIMIT $1 FOR UPDATE OF p SKIP LOCKED) '
                'DELETE FROM ' + _name(table) + ' p USING old WHERE p.ctid=old.tid', quote_limit)
        await run('expired_candidates_deleted',
            "WITH old AS (SELECT p.ctid AS tid FROM v2_match_search_candidates p "
            "JOIN v2_fixtures f ON f.id=p.fixture_id WHERE f.kickoff_at<clock_timestamp()-interval '72 hours' "
            "LIMIT $1 FOR UPDATE OF p SKIP LOCKED) DELETE FROM v2_match_search_candidates p "
            "USING old WHERE p.ctid=old.tid", quote_limit)
        await run('events_deleted',
            "WITH old AS (SELECT id FROM v2_telegram_events WHERE raw_content_expired_at IS NOT NULL "
            "LIMIT $1 FOR UPDATE SKIP LOCKED) DELETE FROM v2_telegram_events e USING old WHERE e.id=old.id")
        for table, expired in (
            ('v2_analysis_jobs', "EXISTS(SELECT 1 FROM v2_expert_messages m WHERE m.id=p.message_id AND m.raw_content_expired_at IS NOT NULL)"),
            ('v2_expert_messages', 'p.raw_content_expired_at IS NOT NULL'),
        ):
            safe = await unreferenced(conn, table, cascades=True)
            await run(table + '_deleted',
                'WITH old AS (SELECT p.id FROM ' + _name(table) + ' p WHERE ' + expired + ' AND ' + safe +
                ' LIMIT $1 FOR UPDATE OF p SKIP LOCKED) DELETE FROM ' + _name(table) + ' p USING old WHERE p.id=old.id')
        # A row that still claims to be in play must still be reported. The
        # collector submits every fixture the station lists on each poll and
        # ingestion stamps last_reported_at, throttled to five minutes, so half
        # an hour of silence means the source dropped the row rather than that
        # the row simply never changed. This records only absence: no score,
        # result or cancellation is inferred, and the next accepted snapshot
        # reinstates the row immediately.
        await run('fixtures_stale_retired',
            "WITH old AS (SELECT id FROM v2_fixtures WHERE stale_projection_at IS NULL "
            "AND decision_context_only_at IS NULL AND phase IN ('live','scheduled') "
            "AND last_reported_at<clock_timestamp()-interval '30 minutes' "
            "ORDER BY last_reported_at,id LIMIT $1 FOR UPDATE SKIP LOCKED) "
            "UPDATE v2_fixtures f SET stale_projection_at=clock_timestamp() FROM old WHERE f.id=old.id")
        await run('fixtures_archived',
            "WITH old AS (SELECT id FROM v2_fixtures WHERE decision_context_only_at IS NULL "
            "AND kickoff_at<clock_timestamp()-interval '72 hours' ORDER BY kickoff_at,id "
            "LIMIT $1 FOR UPDATE SKIP LOCKED) UPDATE v2_fixtures f "
            "SET decision_context_only_at=clock_timestamp() FROM old WHERE f.id=old.id")
        safe_quotes = await unreferenced(conn, 'v2_current_quotes')
        await run('quotes_deleted',
            # Drive from expired parents, then use the existing fixture_id
            # index. A UUID-wide merge/anti scan makes cold random reads and
            # keeps scanning current quotes when no expired parents remain.
            "WITH parents AS MATERIALIZED (SELECT f.id FROM v2_fixtures f "
            "WHERE f.decision_context_only_at IS NOT NULL AND EXISTS(SELECT 1 FROM v2_current_quotes q WHERE q.fixture_id=f.id) "
            "ORDER BY f.kickoff_at,f.id LIMIT 100), old AS (SELECT selected.id FROM parents f "
            "CROSS JOIN LATERAL(SELECT p.id FROM v2_current_quotes p WHERE p.fixture_id=f.id AND " + safe_quotes +
            " LIMIT $1 FOR UPDATE OF p SKIP LOCKED) selected LIMIT $1) "
            "DELETE FROM v2_current_quotes p USING old WHERE p.id=old.id", quote_limit)
        # Bound checks, not just successful deletes. Protected historical
        # contexts must not force a scan of every archived parent per batch.
        previous = await conn.fetchval('SELECT last_counts FROM v2_raw_retention_status WHERE singleton')
        previous = json.loads(previous) if isinstance(previous, str) else (previous or {})
        cursor = previous.get('fixture_gc_cursor') or {}
        after_time = datetime.fromisoformat(cursor['kickoff_at']) if cursor else None
        if after_time is not None and after_time.tzinfo is None:
            raise ValueError('retention_cursor_timezone_required')
        after_id = cursor.get('id') if cursor else None
        query = ("SELECT kickoff_at,id FROM v2_fixtures WHERE decision_context_only_at IS NOT NULL "
                 "AND ($1::timestamptz IS NULL OR (kickoff_at,id)>($1,$2::text)) "
                 "ORDER BY kickoff_at,id LIMIT $3")
        parents = await conn.fetch(query, after_time, after_id, limit)
        if not parents and cursor:
            parents = await conn.fetch(query, None, None, limit)
        safe_fixtures = await unreferenced(conn, 'v2_fixtures', correlated=True)
        await run('fixtures_deleted',
            "WITH old AS (SELECT p.id FROM v2_fixtures p WHERE p.id=ANY($1::text[]) "
            "AND p.decision_context_only_at IS NOT NULL AND " + safe_fixtures +
            " FOR UPDATE OF p SKIP LOCKED) DELETE FROM v2_fixtures p USING old WHERE p.id=old.id",
            [parent['id'] for parent in parents])
        counts['fixture_gc_cursor'] = ({'kickoff_at': parents[-1]['kickoff_at'].isoformat(), 'id': parents[-1]['id']}
                                       if parents else None)
        await conn.execute(
            "UPDATE v2_raw_retention_status SET last_completed_at=clock_timestamp(),"
            "last_error_category=NULL,last_counts=$1::jsonb WHERE singleton", json.dumps(counts))
    return counts


async def run():
    dsn = os.environ['DATABASE_URL']
    validate_local_target(dsn)
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, task.cancel)
    while True:
        conn = None
        try:
            conn = await asyncpg.connect(dsn, timeout=5, command_timeout=8,
                server_settings={'application_name': 'platform-v2-local-retention'})
            async with asyncio.timeout(25):
                counts = await sweep(conn, quote_limit=5000)
            log.info('retention_batch %s', json.dumps({k: v for k, v in counts.items() if not k.endswith('_cursor')}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error('retention_failed category=%s', type(exc).__name__)
            if conn is not None and not conn.is_closed():
                try:
                    await conn.execute("UPDATE v2_raw_retention_status SET last_error_category=$1 WHERE singleton",
                        type(exc).__name__, timeout=2)
                except Exception:
                    log.error('retention_status_write_failed')
        finally:
            if conn is not None:
                await conn.close(timeout=3)
        await asyncio.sleep(2)


async def healthy():
    dsn = os.environ['DATABASE_URL']
    validate_local_target(dsn)
    conn = await asyncpg.connect(dsn, timeout=3, command_timeout=3,
        server_settings={'default_transaction_read_only':'on'})
    try:
        return bool(await conn.fetchval(
            "SELECT last_completed_at>now()-interval '120 seconds' AND last_error_category IS NULL "
            "FROM v2_raw_retention_status WHERE singleton"))
    finally:
        await conn.close(timeout=2)


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO)
    if sys.argv[1:] == ['--health']:
        try:
            raise SystemExit(0 if asyncio.run(healthy()) else 1)
        except Exception:
            raise SystemExit(1)
    try:
        asyncio.run(run())
    except asyncio.CancelledError:
        pass
