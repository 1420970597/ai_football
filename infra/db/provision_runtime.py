"""Provision a separate non-owner v2 DML role without changing existing ACLs."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import secrets
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

import asyncpg

from infra.config.reuse_inventory import parse_env, render_compose_env

ROLE_PATTERN = re.compile(r"v2_runtime[a-z0-9_]{0,48}\Z")
LOCK = 'platform-v2-runtime-provision'


class ProvisionError(RuntimeError):
    """Only fixed diagnostic categories, never SQL/connection/credential values."""


def identifier(value: str) -> str:
    return '"' + value.replace('"','""') + '"'


def validate_role(value: str) -> str:
    if not ROLE_PATTERN.fullmatch(value):
        raise ProvisionError('runtime_role_name_invalid')
    return value


def runtime_dsn(source: str, role: str, password: str) -> str:
    validate_role(role)
    parts=urlsplit(source)
    if parts.scheme not in {'postgres','postgresql'} or not parts.hostname or not parts.path.strip('/'):
        raise ProvisionError('database_url_must_be_postgresql_uri')
    if any(key.casefold() in {'user','password','dbname','host','hostaddr','port','service'} for key,_ in parse_qsl(parts.query)):
        raise ProvisionError('database_url_query_identity_override_forbidden')
    host=parts.hostname
    if ':' in host: host=f'[{host}]'
    netloc=f'{quote(role,safe="")}:{quote(password,safe="")}@{host}'
    if parts.port: netloc+=f':{parts.port}'
    return urlunsplit((parts.scheme,netloc,parts.path,parts.query,''))


async def inspect(conn, role: str) -> dict:
    validate_role(role)
    tables=await conn.fetch(
        "SELECT c.oid,c.relname,c.relowner FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relkind IN('r','p') AND left(c.relname,3)='v2_' ORDER BY c.relname"
    )
    if not any(row['relname']=='v2_schema_migrations' for row in tables):
        raise ProvisionError('v2_migrations_not_present')
    public_create=await conn.fetchval(
        "SELECT count(*) FROM pg_namespace n CROSS JOIN LATERAL aclexplode(COALESCE(n.nspacl,acldefault('n',n.nspowner))) a "
        "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema' AND a.grantee=0 AND a.privilege_type='CREATE'"
    )
    old_public=await conn.fetchval(
        "SELECT count(DISTINCT c.oid) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a "
        "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema' "
        "AND (n.nspname<>'public' OR left(c.relname,3)<>'v2_') "
        "AND c.relkind IN('r','p','v','m','f') AND a.grantee=0 "
        "AND a.privilege_type IN('SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER')"
    )
    dangerous_public=await conn.fetchval(
        "SELECT count(DISTINCT c.oid) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a "
        "WHERE n.nspname='public' AND left(c.relname,3)='v2_' AND a.grantee=0 "
        "AND (a.privilege_type IN('TRUNCATE','REFERENCES','TRIGGER') OR "
        "(c.relname='v2_schema_migrations' AND a.privilege_type<>'SELECT'))"
    )
    existing=await conn.fetchrow(
        "SELECT oid,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolinherit,rolcanlogin "
        "FROM pg_roles WHERE rolname=$1",role,
    )
    blockers=[]
    if public_create: blockers.append('public_schema_create_cannot_be_denied_per_role')
    if old_public: blockers.append('public_old_table_access_cannot_be_denied_per_role')
    if dangerous_public: blockers.append('public_v2_excess_privileges_cannot_be_denied_per_role')
    if existing:
        if any(existing[key] for key in ('rolsuper','rolcreatedb','rolcreaterole','rolreplication','rolbypassrls')):
            blockers.append('existing_runtime_role_has_global_privileges')
        memberships=await conn.fetchval('SELECT count(*) FROM pg_auth_members WHERE member=$1',existing['oid'])
        owned=await conn.fetchval("SELECT count(*) FROM pg_shdepend WHERE refclassid='pg_authid'::regclass AND refobjid=$1 AND deptype='o'",existing['oid'])
        if memberships: blockers.append('existing_runtime_role_has_memberships')
        if owned: blockers.append('existing_runtime_role_owns_objects')
        if existing['rolinherit'] or not existing['rolcanlogin']:
            blockers.append('existing_runtime_role_attributes_invalid')
    database_create=await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM pg_database d CROSS JOIN LATERAL aclexplode(COALESCE(d.datacl,acldefault('d',d.datdba))) a "
        "WHERE d.datname=current_database() AND a.grantee=0 AND a.privilege_type='CREATE')"
    )
    if database_create: blockers.append('public_database_create_cannot_be_denied_per_role')
    temporary=await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM pg_database d CROSS JOIN LATERAL aclexplode(COALESCE(d.datacl,acldefault('d',d.datdba))) a "
        "WHERE d.datname=current_database() AND a.grantee=0 AND a.privilege_type='TEMPORARY')"
    )
    sequences=await conn.fetchval(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relkind='S' AND left(c.relname,3)='v2_'"
    )
    if sequences: blockers.append('v2_sequences_need_explicit_contract')
    return {'role_exists':bool(existing),'v2_table_count':len(tables),'business_table_count':len(tables)-1,
            'migration_identity_unchanged':True,'public_temporary_allowed':bool(temporary),
            'blockers':blockers,'ready':not blockers}


async def verify(conn, role: str) -> dict:
    plan=await inspect(conn,role)
    if not plan['role_exists']: raise ProvisionError('runtime_role_not_present')
    if plan['blockers']: raise ProvisionError(plan['blockers'][0])
    checks=await conn.fetchrow(
        "SELECT has_schema_privilege($1,'public','USAGE') AS usage,has_schema_privilege($1,'public','CREATE') AS create,"
        "has_database_privilege($1,current_database(),'CREATE') AS database_create,"
        "has_database_privilege($1,current_database(),'TEMP') AS temporary",role,
    )
    if not checks['usage'] or checks['create'] or checks['database_create']:
        raise ProvisionError('runtime_schema_or_database_privileges_invalid')
    schema_create=await conn.fetchval(
        "SELECT count(*) FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname<>'information_schema' "
        "AND has_schema_privilege($1,oid,'CREATE')",role,
    )
    if schema_create: raise ProvisionError('runtime_has_persistent_schema_create')
    tables=await conn.fetch(
        "SELECT c.relname,has_table_privilege($1,c.oid,'SELECT') s,has_table_privilege($1,c.oid,'INSERT') i,"
        "has_table_privilege($1,c.oid,'UPDATE') u,has_table_privilege($1,c.oid,'DELETE') d,"
        "has_table_privilege($1,c.oid,'TRUNCATE') t,has_table_privilege($1,c.oid,'TRIGGER') tr,"
        "has_table_privilege($1,c.oid,'REFERENCES') r FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relkind IN('r','p') AND left(c.relname,3)='v2_'",role,
    )
    for table in tables:
        expected=(True,False,False,False) if table['relname']=='v2_schema_migrations' else (True,True,True,True)
        if tuple(table[k] for k in ('s','i','u','d'))!=expected or any(table[k] for k in ('t','tr','r')):
            raise ProvisionError('runtime_table_privileges_invalid')
    old=await conn.fetchval(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema' AND c.relkind IN('r','p','v','m','f') "
        "AND (n.nspname<>'public' OR left(c.relname,3)<>'v2_') "
        "AND (has_table_privilege($1,c.oid,'SELECT') OR has_table_privilege($1,c.oid,'INSERT') "
        "OR has_table_privilege($1,c.oid,'UPDATE') OR has_table_privilege($1,c.oid,'DELETE') "
        "OR has_table_privilege($1,c.oid,'TRUNCATE') OR has_table_privilege($1,c.oid,'REFERENCES') "
        "OR has_table_privilege($1,c.oid,'TRIGGER') OR has_any_column_privilege($1,c.oid,'SELECT') "
        "OR has_any_column_privilege($1,c.oid,'INSERT') OR has_any_column_privilege($1,c.oid,'UPDATE'))",role,
    )
    if old: raise ProvisionError('runtime_has_old_table_access')
    return {**plan,'verified':True,'persistent_ddl_allowed':False,'old_table_access':False,
            'migration_table_read_only':True,'temporary_allowed':bool(checks['temporary'])}


async def provision(conn, role: str, password: str | None, *, refresh: bool=False) -> dict:
    validate_role(role)
    async with conn.transaction():
        await conn.execute("SELECT set_config('lock_timeout','5000',true),set_config('statement_timeout','15000',true)")
        await conn.execute('SELECT pg_advisory_xact_lock(hashtext($1))',LOCK)
        plan=await inspect(conn,role)
        if plan['blockers']: raise ProvisionError(plan['blockers'][0])
        if plan['role_exists']!=refresh:
            raise ProvisionError('refresh_requires_existing_role' if refresh else 'runtime_role_already_exists')
        if not refresh:
            if not password or len(password)<32 or any(c in password for c in "'\r\n\x00"):
                raise ProvisionError('generated_runtime_password_invalid')
            await conn.execute("SET LOCAL password_encryption='scram-sha-256'")
            await conn.execute(
                f'CREATE ROLE {identifier(role)} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT '
                f"PASSWORD '{password}'"
            )
        name=identifier(role)
        database=identifier(await conn.fetchval('SELECT current_database()'))
        await conn.execute(f'GRANT CONNECT ON DATABASE {database} TO {name}')
        await conn.execute(f'GRANT USAGE ON SCHEMA public TO {name}')
        rows=await conn.fetch(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relkind IN('r','p') AND left(c.relname,3)='v2_' ORDER BY c.relname"
        )
        for row in rows:
            table='public.'+identifier(row['relname'])
            await conn.execute(f'REVOKE ALL PRIVILEGES ON TABLE {table} FROM {name}')
            grant='SELECT' if row['relname']=='v2_schema_migrations' else 'SELECT,INSERT,UPDATE,DELETE'
            await conn.execute(f'GRANT {grant} ON TABLE {table} TO {name}')
        await conn.execute(f'ALTER ROLE {name} IN DATABASE {database} SET search_path=pg_catalog,public')
        return await verify(conn,role)


def private_path(root: Path, path: Path, *, exists: bool) -> Path:
    root=root.resolve();runtime=root/'.runtime'
    candidate=path if path.is_absolute() else root/path
    if runtime not in candidate.parents or candidate.is_symlink():
        raise ProvisionError('configuration_path_must_be_inside_project_runtime')
    for parent in [candidate.parent,*candidate.parent.parents]:
        if parent==root: break
        if parent.is_symlink(): raise ProvisionError('configuration_symlink_forbidden')
    if candidate.resolve()!=candidate: raise ProvisionError('configuration_path_invalid')
    if exists:
        if not candidate.is_file() or candidate.stat().st_mode & 0o077:
            raise ProvisionError('configuration_file_must_be_private')
    return candidate


def save_private(path: Path, values: dict) -> None:
    missing=[];parent=path.parent
    while not parent.exists():
        missing.append(parent);parent=parent.parent
    for directory in reversed(missing): directory.mkdir(mode=0o700)
    if path.parent.stat().st_mode & 0o077: raise ProvisionError('configuration_directory_must_be_private')
    for parent in path.parents:
        if parent.is_symlink(): raise ProvisionError('configuration_symlink_forbidden')
        if parent.name=='.runtime':
            if parent.stat().st_mode & 0o077: raise ProvisionError('runtime_directory_must_be_private')
            break
    flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0)
    fd=os.open(path,flags,0o600)
    with os.fdopen(fd,'w') as stream:
        stream.write(render_compose_env(values));stream.flush();os.fsync(stream.fileno())


async def main_async(args) -> int:
    root=Path(__file__).resolve().parents[2]
    source=private_path(root,args.source_env,exists=True)
    values=parse_env(source.read_text())
    dsn=values.get('DATABASE_MIGRATION_URL') or values.get('DATABASE_URL')
    if not dsn: raise ProvisionError('migration_connection_not_configured')
    destination=private_path(root,args.output_env,exists=False)
    conn=await asyncpg.connect(dsn,timeout=10,command_timeout=20)
    try:
        if args.action=='plan': result=await inspect(conn,args.role)
        elif args.action=='verify': result=await verify(conn,args.role)
        elif args.action=='refresh': result=await provision(conn,args.role,None,refresh=True)
        else:
            plan=await inspect(conn,args.role)
            if not plan['ready']: raise ProvisionError(plan['blockers'][0])
            if plan['role_exists']: raise ProvisionError('runtime_role_already_exists')
            password=secrets.token_urlsafe(48)
            # Persist recoverable credentials before creating the role; no rotation on retry.
            save_private(destination,{'DATABASE_URL':runtime_dsn(dsn,args.role,password),'DATABASE_MIGRATION_URL':dsn})
            result=await provision(conn,args.role,password)
        print(json.dumps({'action':args.action,**result},ensure_ascii=False))
        return int(not result.get('ready',False))
    finally: await conn.close()


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['plan','apply','verify','refresh'])
    parser.add_argument('--source-env',type=Path,required=True)
    parser.add_argument('--output-env',type=Path,default=Path('.runtime/db/runtime.env'))
    parser.add_argument('--role',default='v2_runtime_app')
    args=parser.parse_args()
    try:
        validate_role(args.role)
        return asyncio.run(main_async(args))
    except Exception as exc:
        # Connection and SQL errors may contain secrets. Never serialize their message.
        category=str(exc) if isinstance(exc,ProvisionError) else type(exc).__name__
        print(json.dumps({'state':'failed','category':category}))
        return 1


if __name__=='__main__': raise SystemExit(main())
