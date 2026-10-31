"""Provisioning against an explicitly configured throwaway PostgreSQL only."""
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit
import uuid

import asyncpg

from app.database import apply_migrations, check_schema
from infra.config.reuse_inventory import parse_env
from infra.db.provision_runtime import (ProvisionError, inspect, private_path, provision,
    runtime_dsn, save_private, verify)


class RuntimeRoleConfigurationTests(unittest.TestCase):
    def test_private_credentials_preserve_transport_and_never_overwrite(self):
        value=runtime_dsn('postgresql://owner:old@db:5432/app?sslmode=require','v2_runtime_test','a'*40)
        self.assertEqual('sslmode=require',urlsplit(value).query)
        self.assertNotIn('owner',value)
        with self.assertRaises(ProvisionError):
            runtime_dsn('postgresql://owner:old@db/app?user=owner','v2_runtime_test','a'*40)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'.runtime/db/runtime.env'
            save_private(path,{'DATABASE_URL':value})
            self.assertEqual(0o600,path.stat().st_mode&0o777)
            self.assertEqual(0o700,path.parent.stat().st_mode&0o777)
            self.assertEqual(value,parse_env(path.read_text())['DATABASE_URL'])
            self.assertEqual(path,private_path(root,path,exists=True))
            with self.assertRaises(FileExistsError): save_private(path,{'DATABASE_URL':'overwrite'})

    def test_external_path_symlink_and_wide_permissions_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);runtime=root/'.runtime';runtime.mkdir(mode=0o700)
            with self.assertRaises(ProvisionError): private_path(root,root/'outside.env',exists=False)
            link=runtime/'link';link.symlink_to(root)
            with self.assertRaises(ProvisionError): private_path(root,link/'bad.env',exists=False)
            path=runtime/'wide.env';path.write_text('DATABASE_URL=fixture');path.chmod(0o644)
            with self.assertRaises(ProvisionError): private_path(root,path,exists=True)


@unittest.skipUnless(os.environ.get('TEST_DATABASE_ADMIN_URL'),'requires isolated admin database')
class RuntimeRoleDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url=os.environ['TEST_DATABASE_ADMIN_URL'];parts=urlsplit(url)
        if parts.hostname not in {'ai-football-v2-pgtest','ai-football-v2-pgverify'} or parts.path!='/postgres':
            raise RuntimeError('only explicit task Docker PostgreSQL permitted')
        self.admin=await asyncpg.connect(url)
        self.name='least_privilege_'+uuid.uuid4().hex
        await self.admin.execute(f'CREATE DATABASE "{self.name}"')
        self.dsn=url.rsplit('/',1)[0]+'/'+self.name
        self.conn=await asyncpg.connect(self.dsn)
        await apply_migrations(self.conn)
        await self.conn.execute('CREATE TABLE legacy_history(id integer PRIMARY KEY,value text)')
        await self.conn.execute("INSERT INTO legacy_history VALUES(1,'retained')")
        self.role='v2_runtime_'+uuid.uuid4().hex
        self.password='isolated-only-'+uuid.uuid4().hex
        self.runtime=None;self.other=[]

    async def asyncTearDown(self):
        if self.runtime: await self.runtime.close()
        await self.conn.close()
        await self.admin.execute(f'DROP DATABASE "{self.name}" WITH (FORCE)')
        await self.admin.execute(f'DROP ROLE IF EXISTS "{self.role}"')
        for role in self.other: await self.admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        await self.admin.close()

    async def create(self):
        result=await provision(self.conn,self.role,self.password)
        self.runtime=await asyncpg.connect(runtime_dsn(self.dsn,self.role,self.password))
        return result

    async def denied(self,statement):
        with self.assertRaises(asyncpg.InsufficientPrivilegeError):
            await self.runtime.execute(statement)

    async def test_dml_and_schema_read_allowed_old_tables_ddl_and_migration_write_denied(self):
        before=await self.conn.fetchrow("SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user")
        result=await self.create()
        self.assertTrue(result['verified']);await check_schema(self.runtime)
        identity=uuid.uuid4()
        await self.runtime.execute("INSERT INTO v2_membership_plans(id,code,name,price,duration_days) VALUES($1,'isolated','test',1,30)",identity)
        await self.runtime.execute('UPDATE v2_membership_plans SET price=2 WHERE id=$1',identity)
        self.assertEqual(2,await self.runtime.fetchval('SELECT price FROM v2_membership_plans WHERE id=$1',identity))
        await self.runtime.execute('DELETE FROM v2_membership_plans WHERE id=$1',identity)
        denied=['CREATE TABLE public.forbidden(id int)','CREATE SCHEMA forbidden',
            'ALTER TABLE v2_users ADD COLUMN forbidden text','DROP TABLE v2_membership_plans',
            'TRUNCATE v2_sessions','CREATE DATABASE forbidden','CREATE ROLE forbidden',
            'ALTER TABLE legacy_history ADD COLUMN forbidden text','SELECT * FROM legacy_history',
            "INSERT INTO legacy_history VALUES(2,'bad')","UPDATE legacy_history SET value='bad'",
            'DELETE FROM legacy_history',"UPDATE v2_schema_migrations SET checksum='bad'",
            'DELETE FROM v2_schema_migrations',
            "INSERT INTO v2_schema_migrations(version,checksum) VALUES('bad','bad')"]
        for statement in denied:
            with self.subTest(statement=statement): await self.denied(statement)
        attributes=await self.conn.fetchrow('SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolinherit FROM pg_roles WHERE rolname=$1',self.role)
        self.assertFalse(any(attributes.values()))
        self.assertEqual(before,await self.conn.fetchrow('SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user'))
        self.assertEqual('retained',await self.conn.fetchval('SELECT value FROM legacy_history'))

    async def test_temp_reported_without_changing_public_acl(self):
        before=await self.conn.fetchval('SELECT datacl::text FROM pg_database WHERE datname=current_database()')
        result=await self.create();self.assertTrue(result['temporary_allowed'])
        await self.runtime.execute('CREATE TEMP TABLE isolated_temp(id int)')
        after=await self.conn.fetchval('SELECT datacl::text FROM pg_database WHERE datname=current_database()')
        # Direct CONNECT for the new role is the only database ACL addition.
        self.assertIsNone(before)
        self.assertIn('=Tc/',after)

    async def test_public_schema_create_refuses_without_revoking_any_acl(self):
        await self.conn.execute('GRANT CREATE ON SCHEMA public TO PUBLIC')
        plan=await inspect(self.conn,self.role)
        self.assertIn('public_schema_create_cannot_be_denied_per_role',plan['blockers'])
        with self.assertRaises(ProvisionError): await provision(self.conn,self.role,self.password)
        self.assertTrue(await self.conn.fetchval("SELECT has_schema_privilege('public','public','CREATE')"))
        self.assertFalse(await self.conn.fetchval('SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=$1)',self.role))

    async def test_public_old_table_and_migration_write_refused_without_acl_change(self):
        for table,privilege in [('legacy_history','SELECT'),('v2_schema_migrations','UPDATE')]:
            await self.conn.execute(f'GRANT {privilege} ON {table} TO PUBLIC')
            with self.assertRaises(ProvisionError): await provision(self.conn,self.role,self.password)
            self.assertTrue(await self.conn.fetchval("SELECT has_table_privilege('public',$1,$2)",table,privilege))
            await self.conn.execute(f'REVOKE {privilege} ON {table} FROM PUBLIC')

    async def test_existing_privileged_role_never_demoted(self):
        await self.conn.execute(f'CREATE ROLE "{self.role}" LOGIN CREATEDB')
        with self.assertRaises(ProvisionError): await provision(self.conn,self.role,None,refresh=True)
        self.assertTrue(await self.conn.fetchval('SELECT rolcreatedb FROM pg_roles WHERE rolname=$1',self.role))

    async def test_failed_provision_rolls_back_role_and_grants(self):
        with self.assertRaises(ProvisionError):
            await provision(self.conn,self.role,'short')
        self.assertFalse(await self.conn.fetchval('SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=$1)',self.role))

    async def test_refresh_grants_new_v2_table_without_default_old_table_grants(self):
        await self.create()
        await self.conn.execute('CREATE TABLE v2_future(id int PRIMARY KEY)')
        await self.conn.execute('CREATE TABLE legacy_future(id int PRIMARY KEY)')
        await self.denied('SELECT * FROM v2_future')
        await provision(self.conn,self.role,None,refresh=True)
        await self.runtime.execute('INSERT INTO v2_future VALUES(1)')
        await self.denied('SELECT * FROM legacy_future')
        await self.denied('CREATE TABLE v2_forbidden(id int)')
        self.assertTrue((await verify(self.conn,self.role))['verified'])

    async def test_membership_and_object_ownership_refused(self):
        await self.create()
        parent='isolated_parent_'+uuid.uuid4().hex;self.other.append(parent)
        await self.conn.execute(f'CREATE ROLE "{parent}"')
        await self.conn.execute(f'GRANT "{parent}" TO "{self.role}"')
        with self.assertRaises(ProvisionError): await provision(self.conn,self.role,None,refresh=True)
        await self.conn.execute(f'REVOKE "{parent}" FROM "{self.role}"')
        await self.conn.execute('CREATE TABLE v2_owned_test(id int)')
        await self.conn.execute(f'ALTER TABLE v2_owned_test OWNER TO "{self.role}"')
        with self.assertRaises(ProvisionError): await provision(self.conn,self.role,None,refresh=True)

    async def test_direct_old_table_or_other_schema_create_never_passes_verify(self):
        await self.create()
        await self.conn.execute(f'GRANT SELECT(value) ON legacy_history TO "{self.role}"')
        with self.assertRaises(ProvisionError): await verify(self.conn,self.role)
        await self.conn.execute(f'REVOKE SELECT(value) ON legacy_history FROM "{self.role}"')
        await self.conn.execute('CREATE SCHEMA isolated_other')
        await self.conn.execute(f'GRANT CREATE ON SCHEMA isolated_other TO "{self.role}"')
        with self.assertRaises(ProvisionError): await verify(self.conn,self.role)
