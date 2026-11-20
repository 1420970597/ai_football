"""Real API/worker processes on an explicitly temporary PostgreSQL database."""
import asyncio
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from urllib.parse import urlsplit, urlunsplit
import uuid

import asyncpg
import httpx


ADMIN_URL=os.environ.get('TEST_DATABASE_ADMIN_URL','')


@unittest.skipUnless(ADMIN_URL,'requires explicit task-owned temporary PostgreSQL')
class MultiprocessStartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parts=urlsplit(ADMIN_URL)
        allowed=parts.hostname in {'pgtest','ai-football-v2-pgtest','ai-football-v2-pgverify'}
        if parts.hostname=='127.0.0.1':
            allowed=os.environ.get('STARTUP_TEST_ISOLATED_LOOPBACK')=='1'
        if not allowed or parts.username!='postgres' or parts.path!='/postgres' or parts.query or parts.fragment:
            raise RuntimeError('startup test only permits an explicit isolated Docker administrator')
        self.parts=parts
        self.name='test_process_startup_'+uuid.uuid4().hex
        self.control=await asyncpg.connect(ADMIN_URL)
        await self.control.execute('CREATE DATABASE '+self.name)
        self.url=urlunsplit((parts.scheme,parts.netloc,'/'+self.name,'',''))
        self.conn=await asyncpg.connect(self.url)
        self.roles=['startup_'+uuid.uuid4().hex for _ in range(3)]
        self.processes=[]
        for role in self.roles:
            await self.conn.execute('CREATE ROLE '+role+' LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT')
            await self.conn.execute('GRANT CONNECT ON DATABASE '+self.name+' TO '+role)
            await self.conn.execute('GRANT USAGE ON SCHEMA public TO '+role)
            self.assertFalse(await self.conn.fetchval(
                "SELECT has_schema_privilege($1,'public','CREATE')",role))

    async def asyncTearDown(self):
        try:
            await self.stop_all()
            for role in self.roles:
                await self.conn.execute('DROP OWNED BY '+role)
            await self.conn.close()
            await self.control.execute('DROP DATABASE '+self.name+' WITH (FORCE)')
            for role in self.roles:
                await self.control.execute('DROP ROLE '+role)
        finally:
            await self.control.close()

    def environment(self,url):
        root=Path(__file__).resolve().parents[1]
        # Same public dummy secrets as test_config; no inherited provider,
        # bootstrap administrator, proxy, storage or production configuration.
        return {'PATH':os.environ.get('PATH','/usr/local/bin:/usr/bin:/bin'),
                'PYTHONPATH':os.pathsep.join(map(str,(root/'services/api',root/'services',root))),
                'PYTHONDONTWRITEBYTECODE':'1','PYTHONUNBUFFERED':'1','DATABASE_URL':url,
                'APP_SESSION_SECRET':'01'*32,'DATA_ENCRYPTION_KEY':'02'*32,
                'EXPERT_MCP_TOKEN':'test-mcp','COLLECTOR_INTERNAL_TOKEN':'test-collector',
                'HERMES_API_KEY':'test-worker-only','HERMES_API_URL':'http://127.0.0.1:9/v1',
                'REVIEW_CONFIG_URL':'http://127.0.0.1:9/internal/review/config',
                'LIVE_ORDERS_ENABLED':'false','BOOTSTRAP_ADMIN_EMAIL':'','BOOTSTRAP_ADMIN_PASSWORD':''}

    async def process(self,args,url):
        output=tempfile.TemporaryFile()
        process=await asyncio.create_subprocess_exec(sys.executable,*args,env=self.environment(url),
            cwd=Path(__file__).resolve().parents[1],stdout=output,stderr=output)
        self.processes.append((process,output))
        return process,output

    def role_url(self,role):
        host=self.parts.hostname
        return f'postgresql://{role}@{host}:{self.parts.port or 5432}/{self.name}'

    def port(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            return sock.getsockname()[1]

    async def start_services(self):
        ports=[self.port(),self.port()]
        while ports[0]==ports[1]:
            ports[1]=self.port()
        definitions=[(['-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(port),
                       '--loop','asyncio','--http','h11','--no-access-log'],self.role_url(role))
                     for port,role in zip(ports,self.roles[:2])]
        definitions.append((['-m','app.worker'],self.role_url(self.roles[2])))
        services=await asyncio.gather(*(self.process(args,url) for args,url in definitions))
        self.assertEqual(3,len({process.pid for process,_ in services}))
        return ports,services

    async def stop_all(self):
        for process,_ in self.processes:
            if process.returncode is None:
                process.terminate()
        for process,output in self.processes:
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(),5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            output.close()
        self.processes.clear()

    async def assert_no_process_connections(self):
        for _ in range(50):
            count=await self.conn.fetchval(
                'SELECT count(*) FROM pg_stat_activity WHERE datname=$1 AND usename=ANY($2::text[])',self.name,self.roles)
            if count==0:
                return
            await asyncio.sleep(.1)
        self.assertEqual(0,count,'child processes must release every PostgreSQL connection')

    async def schema_state(self):
        tables=await self.conn.fetch(
            "SELECT c.relname,pg_get_userbyid(c.relowner) owner FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN('r','i','S') ORDER BY c.relname")
        versions=await self.conn.fetch('SELECT version,checksum,applied_at FROM v2_schema_migrations ORDER BY version')
        return [tuple(row) for row in tables],[tuple(row) for row in versions]

    async def test_controlled_migration_then_two_apis_and_worker_without_runtime_ddl(self):
        migration,output=await self.process(['-m','app.migrate'],self.url)
        self.assertEqual(0,await asyncio.wait_for(migration.wait(),20))
        output.seek(0)
        self.assertNotIn(b'Traceback',output.read())
        for role in self.roles:
            await self.conn.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO '+role)
            await self.conn.execute('REVOKE INSERT,UPDATE,DELETE ON v2_schema_migrations FROM '+role)
            isolated=await asyncpg.connect(self.role_url(role))
            try:
                with self.assertRaises(asyncpg.InsufficientPrivilegeError):
                    await isolated.execute('CREATE TABLE public.startup_forbidden(id int)')
                with self.assertRaises(asyncpg.InsufficientPrivilegeError):
                    await isolated.execute("UPDATE v2_schema_migrations SET checksum='invalid'")
            finally:
                await isolated.close()
        before=await self.schema_state()
        ports,services=await self.start_services()
        deadline=time.monotonic()+30
        ready=set()
        async with httpx.AsyncClient(timeout=1,trust_env=False) as http:
            while len(ready)<2 and time.monotonic()<deadline:
                for index,port in enumerate(ports):
                    try:
                        response=await http.get(f'http://127.0.0.1:{port}/readyz')
                        if response.status_code==200 and response.json()=={'status':'ready'}:
                            ready.add(index)
                    except httpx.HTTPError:
                        pass
                self.assertTrue(all(p.returncode is None for p,_ in services),'a real service exited during startup')
                await asyncio.sleep(.1)
        self.assertEqual({0,1},ready)
        worker_role=self.roles[2]
        first=None
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            first=await self.conn.fetchval(
                'SELECT max(query_start) FROM pg_stat_activity WHERE datname=$1 AND usename=$2',self.name,worker_role)
            if first:
                break
            await asyncio.sleep(.1)
        self.assertIsNotNone(first,'worker must open its independent runtime pool')
        await asyncio.sleep(1.25)
        latest=await self.conn.fetchval(
            'SELECT max(query_start) FROM pg_stat_activity WHERE datname=$1 AND usename=$2',self.name,worker_role)
        self.assertGreater(latest,first,'worker must poll its real lease loop, not merely stay alive')
        self.assertEqual(0,await self.conn.fetchval('SELECT count(*) FROM v2_analysis_jobs'))
        self.assertEqual(before,await self.schema_state())
        await self.stop_all()
        await self.assert_no_process_connections()

    async def test_unmigrated_schema_rejects_all_services_without_creating_tables(self):
        _,services=await self.start_services()
        results=await asyncio.gather(*(asyncio.wait_for(p.wait(),30) for p,_ in services))
        self.assertTrue(all(result!=0 for result in results),results)
        for _,output in services:
            output.seek(0)
            self.assertIn(b'database migration required',output.read())
        self.assertEqual(0,await self.conn.fetchval(
            "SELECT count(*) FROM pg_tables WHERE schemaname='public'"))
        await self.stop_all()
        await self.assert_no_process_connections()
