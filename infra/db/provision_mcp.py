"""Provision one non-owner, least-privilege role for the existing review tools."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re
import secrets

import asyncpg

from infra.config.reuse_inventory import parse_env
from infra.db.provision_runtime import (ProvisionError, identifier, inspect as inspect_runtime,
                                       private_path, runtime_dsn, save_private)

READ_TABLES = frozenset({'v2_schema_migrations', 'v2_analysis_jobs', 'v2_expert_messages',
    'v2_experts', 'v2_expert_sources', 'v2_source_experts', 'v2_analysis_traces', 'v2_fixtures',
    'v2_current_quotes', 'v2_match_searches', 'v2_match_search_candidates', 'v2_review_fixture_bindings',
    'v2_review_market_reads', 'v2_expert_signals', 'v2_quote_evidence'})
INSERT_TABLES = frozenset({'v2_analysis_traces', 'v2_match_searches', 'v2_match_search_candidates',
    'v2_review_fixture_bindings', 'v2_review_market_reads', 'v2_expert_signals', 'v2_quote_evidence'})
UPDATE_TABLES = frozenset({'v2_match_searches', 'v2_review_fixture_bindings', 'v2_review_market_reads'})
LOCK_COLUMNS = {'v2_analysis_jobs': 'id', 'v2_fixtures': 'id', 'v2_current_quotes': 'id'}
ROLE_PATTERN = re.compile(r'v2_runtime_mcp[a-z0-9_]{0,44}\Z')
LOCK = 'platform-v2-mcp-provision'


def validate_role(role):
    if not ROLE_PATTERN.fullmatch(role):
        raise ProvisionError('mcp_role_name_invalid')
    return role


async def inspect(conn, role):
    validate_role(role)
    plan = await inspect_runtime(conn, role)
    present = set(await conn.fetchval("SELECT array_agg(relname::text) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN('r','p')") or [])
    if READ_TABLES - present or not await conn.fetchval("SELECT to_regclass('v2_mcp.v2_admin_settings') IS NOT NULL"):
        plan['blockers'].append('mcp_schema_migration_required')
    # A restrictive role cannot override privileges inherited from PUBLIC.
    public_grants = await conn.fetchval("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a "
        "WHERE n.nspname='public' AND left(c.relname,3)='v2_' AND a.grantee=0")
    if public_grants:
        plan['blockers'].append('public_business_grants_cannot_be_restricted')
    plan['ready'] = not plan['blockers']
    return plan


async def verify(conn, role):
    plan = await inspect(conn, role)
    if plan['blockers'] or not plan['role_exists']:
        raise ProvisionError(plan['blockers'][0] if plan['blockers'] else 'mcp_role_not_present')
    rows = await conn.fetch("SELECT c.oid,n.nspname,c.relname,"
        "has_table_privilege($1,c.oid,'SELECT') AS s,has_table_privilege($1,c.oid,'INSERT') AS i,"
        "has_table_privilege($1,c.oid,'UPDATE') AS u,has_table_privilege($1,c.oid,'DELETE') AS d,"
        "has_table_privilege($1,c.oid,'TRUNCATE') AS t,has_table_privilege($1,c.oid,'TRIGGER') AS tr,"
        "has_table_privilege($1,c.oid,'REFERENCES') AS r "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema' AND c.relkind IN('r','p','v','m','f')", role)
    for row in rows:
        table = row['relname']
        is_public = row['nspname'] == 'public'
        is_view = row['nspname'] == 'v2_mcp' and table == 'v2_admin_settings'
        for field, allowed in (('s', is_view or is_public and table in READ_TABLES),
                ('i', is_public and table in INSERT_TABLES), ('u', is_public and table in UPDATE_TABLES),
                ('d', False), ('t', False), ('tr', False), ('r', False)):
            if row[field] != allowed:
                raise ProvisionError('mcp_table_privileges_invalid')
    columns = await conn.fetch("SELECT n.nspname,c.relname,a.attname,"
        "has_column_privilege($1,c.oid,a.attnum,'SELECT') AS s,"
        "has_column_privilege($1,c.oid,a.attnum,'INSERT') AS i,"
        "has_column_privilege($1,c.oid,a.attnum,'UPDATE') AS u "
        "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema' "
        "AND c.relkind IN('r','p','v','m','f') AND a.attnum>0 AND NOT a.attisdropped", role)
    for column in columns:
        table = column['relname']
        is_public = column['nspname'] == 'public'
        is_view = column['nspname'] == 'v2_mcp' and table == 'v2_admin_settings'
        for field, expected in (('s', is_view or is_public and table in READ_TABLES),
                ('i', is_public and table in INSERT_TABLES),
                ('u', is_public and (table in UPDATE_TABLES or LOCK_COLUMNS.get(table) == column['attname']))):
            if column[field] != expected:
                raise ProvisionError('mcp_column_privileges_invalid')
    if await conn.fetchval("SELECT count(*) FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' "
            "AND nspname<>'information_schema' AND has_schema_privilege($1,oid,'CREATE')", role):
        raise ProvisionError('mcp_schema_create_forbidden')
    if await conn.fetchval("SELECT has_database_privilege($1,current_database(),'CREATE')", role):
        raise ProvisionError('mcp_database_create_forbidden')
    if not await conn.fetchval("SELECT has_schema_privilege($1,'v2_mcp','USAGE')", role):
        raise ProvisionError('mcp_policy_view_not_readable')
    return {**plan, 'verified': True, 'user_account_order_access': False,
            'encrypted_settings_access': False, 'persistent_ddl_allowed': False,
            'connection_limit': 8, 'review_implementation_shared': True}


async def provision(conn, role, password, *, refresh=False):
    validate_role(role)
    async with conn.transaction():
        await conn.execute("SELECT set_config('lock_timeout','5000',true),set_config('statement_timeout','15000',true)")
        await conn.execute('SELECT pg_advisory_xact_lock(hashtext($1))', LOCK)
        plan = await inspect(conn, role)
        if plan['blockers']:
            raise ProvisionError(plan['blockers'][0])
        if plan['role_exists'] != refresh:
            raise ProvisionError('mcp_role_refresh_mismatch')
        name = identifier(role)
        if not refresh:
            if not password or len(password) < 32 or any(c in password for c in "'\r\n\x00"):
                raise ProvisionError('generated_mcp_password_invalid')
            await conn.execute("SET LOCAL password_encryption='scram-sha-256'")
            await conn.execute(f'CREATE ROLE {name} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                               f"NOREPLICATION NOBYPASSRLS NOINHERIT CONNECTION LIMIT 8 PASSWORD '{password}'")
        db = identifier(await conn.fetchval('SELECT current_database()'))
        await conn.execute(f'GRANT CONNECT ON DATABASE {db} TO {name}')
        await conn.execute(f'GRANT USAGE ON SCHEMA public,v2_mcp TO {name}')
        # Grant only explicit tables. Never refresh or revoke any other role.
        for table in sorted(READ_TABLES):
            quoted = 'public.' + identifier(table)
            privileges = ['SELECT']
            if table in INSERT_TABLES:
                privileges.append('INSERT')
            if table in UPDATE_TABLES:
                privileges.append('UPDATE')
            await conn.execute(f'GRANT {",".join(privileges)} ON TABLE {quoted} TO {name}')
            if table in LOCK_COLUMNS:
                await conn.execute(f'GRANT UPDATE({identifier(LOCK_COLUMNS[table])}) ON TABLE {quoted} TO {name}')
        await conn.execute(f'GRANT SELECT ON v2_mcp.v2_admin_settings TO {name}')
        # Explicit pg_temp last prevents temporary objects shadowing policy.
        await conn.execute(f'ALTER ROLE {name} IN DATABASE {db} SET search_path=pg_catalog,v2_mcp,public,pg_temp')
        return await verify(conn, role)


async def main_async(args):
    root = Path(__file__).resolve().parents[2]
    source = private_path(root, args.source_env, exists=True)
    values = parse_env(source.read_text())
    dsn = values.get('DATABASE_MIGRATION_URL')
    if not dsn:
        raise ProvisionError('migration_connection_not_configured')
    conn = await asyncpg.connect(dsn, timeout=10, command_timeout=20)
    try:
        if args.action == 'plan':
            result = await inspect(conn, args.role)
        elif args.action == 'verify':
            result = await verify(conn, args.role)
        elif args.action == 'refresh':
            result = await provision(conn, args.role, None, refresh=True)
        else:
            plan = await inspect(conn, args.role)
            if not plan['ready'] or plan['role_exists']:
                raise ProvisionError('mcp_provision_not_ready')
            destination = private_path(root, args.output_env, exists=False)
            password = secrets.token_urlsafe(48)
            # Recoverable new role credential only; never export the admin DSN.
            save_private(destination, {'DATABASE_URL': runtime_dsn(dsn, args.role, password)})
            result = await provision(conn, args.role, password)
        print(json.dumps({'action': args.action, **result}))
        return int(not result['ready'])
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'apply', 'verify', 'refresh'])
    parser.add_argument('--source-env', type=Path, required=True)
    parser.add_argument('--output-env', type=Path, default=Path('.runtime/db/mcp.env'))
    parser.add_argument('--role', default='v2_runtime_mcp')
    args = parser.parse_args()
    try:
        validate_role(args.role)
        return asyncio.run(main_async(args))
    except Exception as exc:
        category = str(exc) if isinstance(exc, ProvisionError) else type(exc).__name__
        print(json.dumps({'state': 'failed', 'category': category}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
