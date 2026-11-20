"""仅在本任务已恢复的隔离目标库演练闭盘保留策略；无生产连接或 CLI。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import uuid

from .bundle import RecoveryError, audit_inventory, canonical, isolated_connection, media_references

MAX_CANDIDATES = 500
MIN_RETENTION_DAYS = 30
MAX_AUDIT_ROWS = 100_000
MAX_AUDIT_BYTES = 64 * 1024 * 1024
FORMAT = 'isolated-closed-quote-retention-1'
TARGET_NAME = r'recovery_[a-f0-9]{32}_target_test'


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z_][a-z0-9_]*', value):
        raise RecoveryError('retention_identifier_unrecognized')
    return '"' + value + '"'


def _table(schema, name):
    return _identifier(schema) + '.' + _identifier(name)


def _sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


async def _target(conn):
    # Reuse the restoration guard, then narrow mutation to a named target. A
    # source/production connection is rejected before any locks or DML.
    await isolated_connection(conn)
    name = await conn.fetchval('SELECT current_database()')
    if not re.fullmatch(TARGET_NAME, name):
        raise RecoveryError('retention_requires_owned_recovery_target')
    return name


async def _budget(conn):
    await conn.execute("SET LOCAL statement_timeout='5000'")
    await conn.execute("SET LOCAL lock_timeout='1000'")


async def _contract(conn):
    tables = await conn.fetch("SELECT c.relname AS name FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relkind IN('r','p') AND left(c.relname,3)='v2_' ORDER BY c.relname")
    names = [row['name'] for row in tables]
    if 'v2_current_quotes' not in names:
        raise RecoveryError('platform_schema_missing')
    for name in names:
        if not re.fullmatch(r'v2_[a-z0-9_]+', name):
            raise RecoveryError('retention_schema_unrecognized')
    # JSON evidence in any v2 column is protected, including nested/array and
    # future columns. Direct incoming foreign keys are not limited to v2.
    json_columns = [dict(row) for row in await conn.fetch(
        "SELECT c.relname AS table_name,a.attname AS column_name FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=c.oid "
        "WHERE n.nspname='public' AND c.relname=ANY($1::text[]) AND a.attnum>0 AND NOT a.attisdropped "
        "AND a.atttypid IN('json'::regtype,'jsonb'::regtype) ORDER BY c.relname,a.attname", names)]
    fixture_tables = [row['table_name'] for row in await conn.fetch(
        "SELECT c.relname AS table_name FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid=c.oid WHERE n.nspname='public' AND c.relname=ANY($1::text[]) "
        "AND a.attname='fixture_id' AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname", names)
        if row['table_name'] != 'v2_current_quotes']
    foreign_keys = [dict(row) for row in await conn.fetch(
        "SELECT n.nspname AS schema_name,c.relname AS table_name,"
        "array_agg(a.attname ORDER BY k.ord) AS child_columns,array_agg(b.attname ORDER BY k.ord) AS parent_columns "
        "FROM pg_constraint f JOIN pg_class c ON c.oid=f.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
        "CROSS JOIN LATERAL unnest(f.conkey,f.confkey) WITH ORDINALITY k(child,parent,ord) "
        "JOIN pg_attribute a ON a.attrelid=f.conrelid AND a.attnum=k.child "
        "JOIN pg_attribute b ON b.attrelid=f.confrelid AND b.attnum=k.parent "
        "WHERE f.contype='f' AND f.confrelid='public.v2_current_quotes'::regclass "
        "GROUP BY f.oid,n.nspname,c.relname ORDER BY n.nspname,c.relname,f.oid")]
    for item in json_columns:
        _identifier(item['column_name'])
    for fk in foreign_keys:
        _table(fk['schema_name'], fk['table_name'])
        for name in [*fk['child_columns'], *fk['parent_columns']]:
            _identifier(name)
    return {'tables': names, 'json_columns': json_columns,
            'fixture_tables': fixture_tables, 'foreign_keys': foreign_keys}


async def _checkpoint(conn, contract, excluded_quotes=()):
    # All rows, not a representative sample. The budget fails closed instead
    # of silently truncating a table. Only counts/hashes leave this function.
    table_hashes = {}
    total_rows = total_bytes = 0
    for name in contract['tables']:
        hashes = []
        query = 'SELECT to_jsonb(t) AS value FROM ' + _table('public', name) + ' t'
        args = ()
        if name == 'v2_current_quotes':
            query += ' WHERE NOT(id=ANY($1::uuid[]))'
            args = (list(excluded_quotes),)
        async for row in conn.cursor(query, *args):
            raw = canonical(_json(row['value']))
            total_rows += 1
            total_bytes += len(raw)
            if total_rows > MAX_AUDIT_ROWS or total_bytes > MAX_AUDIT_BYTES:
                raise RecoveryError('retention_full_audit_budget_exceeded')
            hashes.append(hashlib.sha256(raw).hexdigest())
        table_hashes[name] = {'rows': len(hashes), 'sha256': _sha(sorted(hashes))}
    return {'inventory': await audit_inventory(conn), 'table_hashes': table_hashes,
            'media_references_sha256': _sha(await media_references(conn)),
            'account_balances': dict(await conn.fetchrow(
                "SELECT count(*) AS accounts,count(*) FILTER(WHERE available_balance IS NULL) AS unknown_balances,"
                "sum(available_balance)::text AS known_balance_total FROM v2_leyu_accounts"))}


async def _references(conn, contract, ids):
    protected = set()
    if not ids:
        return protected
    for fk in contract['foreign_keys']:
        join = ' AND '.join('r.' + _identifier(child) + '=q.' + _identifier(parent)
                           for child, parent in zip(fk['child_columns'], fk['parent_columns']))
        rows = await conn.fetch('SELECT DISTINCT q.id FROM public.v2_current_quotes q JOIN '
            + _table(fk['schema_name'], fk['table_name']) + ' r ON ' + join + ' WHERE q.id=ANY($1::uuid[])', ids)
        protected.update(row['id'] for row in rows)
    # A bound signal/result/order path need not contain the quote UUID. Preserve
    # every quote of an explicitly referenced fixture, conservatively.
    for name in contract['fixture_tables']:
        rows = await conn.fetch('SELECT DISTINCT q.id FROM public.v2_current_quotes q JOIN '
            + _table('public', name) + ' r ON r.fixture_id=q.fixture_id WHERE q.id=ANY($1::uuid[])', ids)
        protected.update(row['id'] for row in rows)
    for item in contract['json_columns']:
        text = 'lower(to_jsonb(r.' + _identifier(item['column_name']) + ')::text)'
        # Substring matching intentionally over-preserves ambiguous references.
        # It also catches uppercase/compact UUIDs and values embedded in links.
        rows = await conn.fetch('SELECT DISTINCT q.id FROM public.v2_current_quotes q JOIN '
            + _table('public', item['table_name']) + ' r ON ('
            + 'strpos(' + text + ',q.id::text)>0 OR '
            + 'strpos(' + text + ",replace(q.id::text,'-',''))>0 OR "
            + 'strpos(' + text + ',lower(q.fixture_id))>0) WHERE q.id=ANY($1::uuid[])', ids)
        protected.update(row['id'] for row in rows)
    return protected


ELIGIBLE = ("q.status='closed' AND f.phase IN('finished','cancelled') "
    "AND q.source_time_quality='known' AND f.source_time_quality='known' "
    "AND q.source_updated_at IS NOT NULL AND f.source_updated_at IS NOT NULL "
    "AND q.source_updated_at < $1 AND q.received_at < $1 "
    "AND f.source_updated_at < $1 AND f.received_at < $1 AND f.kickoff_at < $1")


async def plan_retention(conn, *, cutoff: datetime, limit: int = MAX_CANDIDATES, after: uuid.UUID | None = None):
    database = await _target(conn)
    if (not isinstance(cutoff, datetime) or cutoff.tzinfo is None
            or cutoff > datetime.now(timezone.utc) - timedelta(days=MIN_RETENTION_DAYS)
            or type(limit) is not int or not 1 <= limit <= MAX_CANDIDATES
            or after is not None and not isinstance(after, uuid.UUID)):
        raise RecoveryError('retention_policy_or_candidate_budget_invalid')
    async with asyncio.timeout(30), conn.transaction(isolation='repeatable_read', readonly=True):
        await _budget(conn)
        contract = await _contract(conn)
        rows = await conn.fetch('SELECT q.id,q.fixture_id,to_jsonb(q) AS quote,to_jsonb(f) AS fixture '
            'FROM public.v2_current_quotes q JOIN public.v2_fixtures f ON f.id=q.fixture_id WHERE '
            + ELIGIBLE + ' AND ($2::uuid IS NULL OR q.id>$2) ORDER BY q.id LIMIT $3', cutoff, after, limit + 1)
        scanned = rows[:limit]
        protected = await _references(conn, contract, [row['id'] for row in scanned])
        manifest = {'format': FORMAT, 'database': database, 'cutoff': cutoff.astimezone(timezone.utc).isoformat(),
            'rows': [{'id': str(row['id']), 'fixture_id': row['fixture_id'],
                'original_sha256': _sha({'quote': _json(row['quote']), 'fixture': _json(row['fixture'])})}
                for row in scanned if row['id'] not in protected],
            'scanned': len(scanned), 'protected': len(protected), 'has_more': len(rows) > limit,
            'next_after': str(scanned[-1]['id']) if scanned else None,
            'contract_sha256': _sha(contract), 'protected_checkpoint': await _checkpoint(
                conn, contract, [row['id'] for row in scanned if row['id'] not in protected])}
        return {**manifest, 'manifest_sha256': _sha(manifest)}


def _validate(manifest, database):
    if not isinstance(manifest, dict):
        raise RecoveryError('retention_manifest_invalid')
    copy = {key: value for key, value in manifest.items() if key != 'manifest_sha256'}
    if (manifest.get('format') != FORMAT or manifest.get('database') != database
            or manifest.get('manifest_sha256') != _sha(copy)
            or not isinstance(manifest.get('rows'), list) or len(manifest['rows']) > MAX_CANDIDATES):
        raise RecoveryError('retention_manifest_invalid')
    try:
        cutoff = datetime.fromisoformat(manifest['cutoff'])
        if cutoff.tzinfo is None or cutoff > datetime.now(timezone.utc) - timedelta(days=MIN_RETENTION_DAYS):
            raise ValueError()
        ids = [uuid.UUID(row['id']) for row in manifest['rows']]
        if ids != sorted(set(ids)) or any(str(identity) != row['id']
                or not isinstance(row['fixture_id'], str) or not row['fixture_id']
                or not re.fullmatch('[a-f0-9]{64}', row['original_sha256'])
                for identity, row in zip(ids, manifest['rows'])):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise RecoveryError('retention_manifest_invalid') from None
    return cutoff, ids


async def apply_retention(conn, manifest):
    database = await _target(conn)
    cutoff, ids = _validate(manifest, database)
    async with asyncio.timeout(30), conn.transaction():
        await _budget(conn)
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('isolated-platform-recovery'))")
        contract = await _contract(conn)
        if _sha(contract) != manifest['contract_sha256']:
            raise RecoveryError('retention_reference_contract_changed')
        # This intentionally serializes the restored rehearsal. Locks protect
        # all JSON/financial evidence writers, not just the quote FK. No runtime
        # service receives permission or a route to execute this operation.
        relations = {('public', name) for name in contract['tables']}
        relations.update((fk['schema_name'], fk['table_name']) for fk in contract['foreign_keys'])
        await conn.execute('LOCK TABLE ' + ','.join(_table(*item) for item in sorted(relations))
                           + ' IN SHARE ROW EXCLUSIVE MODE')
        if _sha(await _contract(conn)) != manifest['contract_sha256']:
            raise RecoveryError('retention_reference_contract_changed')
        before = await _checkpoint(conn, contract, ids)
        rows = await conn.fetch('SELECT q.id,q.fixture_id,to_jsonb(q) AS quote,to_jsonb(f) AS fixture '
            'FROM public.v2_current_quotes q JOIN public.v2_fixtures f ON f.id=q.fixture_id '
            'WHERE q.id=ANY($1::uuid[]) ORDER BY q.id FOR UPDATE OF q,f', ids)
        if not rows:
            return {'state': 'already_absent' if ids else 'no_candidates', 'deleted': 0,
                    'manifest_sha256': manifest['manifest_sha256'], 'before': before, 'after': before}
        if len(rows) != len(ids):
            raise RecoveryError('retention_candidate_missing')
        expected = {row['id']: row for row in manifest['rows']}
        for row in rows:
            digest = _sha({'quote': _json(row['quote']), 'fixture': _json(row['fixture'])})
            if (expected[str(row['id'])]['original_sha256'] != digest
                    or expected[str(row['id'])]['fixture_id'] != row['fixture_id']):
                raise RecoveryError('retention_original_row_changed')
        eligible = await conn.fetch('SELECT q.id FROM public.v2_current_quotes q '
            'JOIN public.v2_fixtures f ON f.id=q.fixture_id WHERE ' + ELIGIBLE
            + ' AND q.id=ANY($2::uuid[])', cutoff, ids)
        if len(eligible) != len(ids):
            raise RecoveryError('retention_candidate_no_longer_eligible')
        if await _references(conn, contract, ids):
            raise RecoveryError('retention_new_reference_conflict')
        if before != manifest['protected_checkpoint']:
            raise RecoveryError('retention_protected_state_changed')
        deleted = await conn.fetch('DELETE FROM public.v2_current_quotes WHERE id=ANY($1::uuid[]) RETURNING id', ids)
        if len(deleted) != len(ids):
            raise RecoveryError('retention_delete_count_conflict')
        after = await _checkpoint(conn, contract, ids)
        if before != after:
            raise RecoveryError('retention_protected_state_changed')
        return {'state': 'rehearsed', 'deleted': len(deleted), 'manifest_sha256': manifest['manifest_sha256'],
                'before': before, 'after': after}
