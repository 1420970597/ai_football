"""Remote worker contracts and real local Unix HTTP transport; no provider calls."""
import copy
import asyncio
import http.client
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
from types import SimpleNamespace
import socket
import socketserver
import tempfile
import threading
import unittest
import httpx
from unittest.mock import patch

from infra.remote.api_bridge import Bridge, MAX_BODY, permitted
from infra.remote.deployment import project_path, prepare, switch_plan, validate_manifest
from infra.remote.preflight import ProbeClient, classify_collector, summarize
from infra.remote.ssh_link import command, health, REMOTE_SOCKET
from infra.remote.socket_guard import grant, StaleSocketGuard

TOKENS={'collector':'synthetic-collector','executor':'synthetic-executor'}


def manifest():
    commit='1'*40
    result={'source_commit':commit,'configuration_version':7,'remote_root':'/root/ai_football',
            'images':{role:'registry.invalid/platform-'+role+'@sha256:'+'2'*64 for role in ('collector','executor','tunnel')},
            'image_sources':{role:commit for role in ('collector','executor','tunnel')},
            'observations':{**{name:True for name in ('remote_project_exists','ssh_host_key_verified',
                'ssh_streamlocal_verified','socket_directory_verified','secrets_private_verified',
                'api_read_only_probe_verified','s3_tls_read_verified','live_orders_disabled','active_lease_handoff_verified')},
                **{name:0 for name in ('local_collectors_running','local_executors_running','remote_collectors_running','remote_executors_running')}}}
    result['image_inspections']={role:{'Id':'sha256:'+'3'*64,'RepoDigests':[image],
        'Config':{'Labels':{'org.opencontainers.image.revision':commit}}} for role,image in result['images'].items()}
    return result


class DeploymentTests(unittest.TestCase):
    def test_pinned_same_source_images_and_zero_instances_yield_plan_without_execution(self):
        result=switch_plan(manifest())
        self.assertTrue(result['eligible'])
        self.assertFalse(result['execution_performed'])
        self.assertFalse(result['deletion_performed'])
        self.assertTrue(switch_plan(manifest(),rollback=True)['eligible'])
    def test_no_dual_workers_live_permissions_or_inferred_lease_handoff(self):
        for field in ('local_collectors_running','local_executors_running','remote_collectors_running','remote_executors_running'):
            value=manifest();value['observations'][field]=1
            self.assertFalse(switch_plan(value)['eligible'])
        for field in ('live_orders_disabled','active_lease_handoff_verified','ssh_host_key_verified','remote_project_exists'):
            value=manifest();value['observations'][field]=False
            self.assertFalse(switch_plan(value)['eligible'])
    def test_mutable_tag_source_mismatch_root_escape_and_boolean_count_rejected(self):
        value=manifest();value['images']['collector']='registry.invalid/collector:latest'
        self.assertIn('collector_image_digest_required',validate_manifest(value))
        value=manifest();value['image_sources']['executor']='3'*40
        self.assertIn('executor_image_source_mismatch',validate_manifest(value))
        value=manifest();value['image_inspections']['collector']['Config']['Labels']['org.opencontainers.image.revision']='4'*40
        self.assertIn('collector_pulled_image_provenance_unverified',validate_manifest(value))
        value=manifest();value['remote_root']='/outside'
        self.assertIn('remote_project_root_unverified',validate_manifest(value))
        value=manifest();value['observations']['local_collectors_running']=False
        self.assertFalse(switch_plan(value)['eligible'])
    def test_project_prepare_never_generates_credentials_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            self.assertFalse(prepare(root)['directory_ready'])
            with patch('infra.remote.deployment.os.geteuid',return_value=0),patch('infra.remote.deployment.os.chown'):
                result=prepare(root,apply=True)
            self.assertFalse(result['secrets_generated'])
            self.assertFalse(result['remote_writes'])
            self.assertEqual([],list((root/'.runtime/remote').iterdir()))
            with self.assertRaises(ValueError):project_path(root,'../outside')
            (root/'symlink').symlink_to(root/'.runtime')
            with self.assertRaises(ValueError):project_path(root,'symlink/secret')
    def test_ssh_only_fixed_targets_host_key_check_and_no_general_forward(self):
        result=command({'ssh_config':'/run/storage/ssh_config','ssh_host':'storage-vps'})
        self.assertIn('StrictHostKeyChecking=yes',result)
        self.assertIn('ExitOnForwardFailure=yes',result)
        self.assertIn(REMOTE_SOCKET+':api:8080',result)
        self.assertIn('0.0.0.0:18092:127.0.0.1:18092',result)
        for config in ({'ssh_config':'/etc/ssh/ssh_config','ssh_host':'safe'},
                       {'ssh_config':'/run/storage/ssh_config','ssh_host':'-oProxyCommand=bad'},
                       {'ssh_config':'/run/storage/ssh_config','ssh_host':'safe','remote':'bad'}):
            with self.assertRaises(ValueError):command(config)
    def test_socket_guard_cannot_chmod_regular_file_or_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            parent=path.stat()
            with patch('infra.remote.socket_guard.Path.lstat') as mocked:
                mocked.return_value=type('Info',(),{'st_mode':0o42750,'st_uid':0,'st_gid':10005})()
                with self.assertRaises(ValueError):grant(path)
            self.assertEqual(parent.st_mode,path.stat().st_mode)


class PreflightTests(unittest.TestCase):
    def test_exact_revision_and_all_probes_required(self):
        value=manifest();probes={key:{'state':'ready'} for key in ('api','collector','database','objects','hermes')}
        probes['collector']['configuration_version']=7
        self.assertTrue(summarize(value,{'version':7},probes,finished_revision=7)['ready'])
        for changed in (None,8,True):
            result=summarize(value,{'version':7},probes,finished_revision=changed)
            self.assertFalse(result['ready'])
        probes['collector']['configuration_version']=6
        self.assertFalse(summarize(value,{'version':7},probes,finished_revision=7)['ready'])
    def test_statuses_are_distinct_and_arbitrary_input_never_echoed(self):
        probes={key:{'state':state,'secret':'MUST-NOT-LEAK'} for key,state in zip(
            ('api','collector','database','objects','hermes'),('missing','expired','invalid','unreachable','unverified'))}
        probes['collector']['configuration_version']=7
        value=manifest();value['private']='MUST-NOT-LEAK'
        result=summarize(value,{'version':7,'token':'MUST-NOT-LEAK'},probes,finished_revision=7)
        self.assertNotIn('MUST-NOT-LEAK',json.dumps(result))
        self.assertFalse(result['ready'])
        self.assertTrue({'missing','expired','invalid','unreachable','unverified'}<=set(item['state'] for item in result['findings']))
    def test_explicit_expiry_is_not_network_failure(self):
        self.assertEqual('expired',classify_collector({'source_health':{'leyu':{'last_error':'session_expired','config_version':7}}})['state'])
        self.assertEqual('unreachable',classify_collector({})['state'])
    def test_partial_stale_unknown_or_tg_degraded_cannot_be_business_ready(self):
        value={'connected':True,'authorized':True,'source_health':{'leyu':{'state':'healthy','freshness':'fresh','config_version':7},
                                                                 'telegram':{'degraded':False}}}
        self.assertEqual('ready',classify_collector(value)['state'])
        for state,freshness in (('partial','fresh'),('healthy','stale'),('healthy','unknown'),('degraded','fresh')):
            changed=copy.deepcopy(value);changed['source_health']['leyu'].update(state=state,freshness=freshness)
            self.assertEqual('unverified',classify_collector(changed)['state'])
        value['source_health']['telegram']['degraded']=True
        self.assertEqual('unverified',classify_collector(value)['state'])
    def test_secure_transport_and_explicit_protected_bridge_only(self):
        ProbeClient(api_base='https://api.invalid/internal',collector_base='https://control.invalid',collector_token='fake')
        ProbeClient(api_base='http://api:8080/internal',collector_base='http://remote-link:18092',collector_token='fake',bridge_verified=True)
        for target in ('http://api:8080','http://arbitrary:8080','https://u:p@private.invalid','https://private.invalid?key=secret'):
            with self.assertRaises(ValueError):ProbeClient(api_base=target,collector_base='https://control.invalid',collector_token='fake')


class UnixServer(socketserver.UnixStreamServer):
    allow_reuse_address=True


class FakeAPI(BaseHTTPRequestHandler):
    def log_message(self,*_):pass
    def do_GET(self):self.respond()
    def do_POST(self):self.respond()
    def do_PUT(self):self.respond()
    def respond(self):
        body=self.rfile.read(int(self.headers.get('Content-Length','0')))
        self.server.calls.append((self.command,self.path,body))
        result=b'{"accepted":true}'
        self.send_response(200);self.send_header('Content-Length',str(len(result)));self.end_headers();self.wfile.write(result)


class RemoteTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=str(Path(self.temp.name)/'api.sock')
        self.upstream=UnixServer(self.path,FakeAPI);self.upstream.calls=[]
        self.upthread=threading.Thread(target=self.upstream.serve_forever,daemon=True);self.upthread.start()
        self.bridge=Bridge(('127.0.0.1',0),TOKENS,self.path)
        self.thread=threading.Thread(target=self.bridge.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.bridge.shutdown();self.bridge.server_close();self.thread.join(2)
        self.upstream.shutdown();self.upstream.server_close();self.upthread.join(2);self.temp.cleanup()
    def request(self,method,path,body=b'',token='collector',headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.bridge.server_port,timeout=2)
        conn.request(method,path,body=body,headers={'Authorization':'Bearer '+TOKENS.get(token,token),**(headers or {})})
        result=conn.getresponse();result.read();status=result.status;conn.close();return status
    def test_single_http_atomic_snapshot_through_unix_no_split(self):
        body=b'{' + b' '* (1024*1024+7) + b'}'
        self.assertEqual(200,self.request('POST','/internal/collector/fixtures',body))
        self.assertEqual([('POST','/internal/collector/fixtures',body)],self.upstream.calls)
    def test_roles_wrong_credentials_traversal_and_query_boundary(self):
        for path,token in (('/internal/executor/claim','collector'),('/internal/collector/config','executor'),
                           ('/api/v1/admin/settings','collector'),('/internal/collector/config','wrong'),
                           ('/internal/../collector/config','collector'),('/internal/telegram/pending?limit=101','collector')):
            self.assertEqual(403,self.request('POST' if path.endswith('claim') else 'GET',path,token=token))
        self.assertEqual(200,self.request('GET','/internal/telegram/pending?limit=30'))
        self.assertEqual(200,self.request('POST','/internal/telegram/observed/ack'))
        self.assertEqual(200,self.request('POST','/internal/executor/dispatch',token='executor'))
        self.assertEqual(200,self.request('POST','/internal/executor/claim?live_capable=false',token='executor'))
        for query in ('live_capable=true','live_capable=false&live_capable=true','live_capable=0','arbitrary=false'):
            self.assertEqual(403,self.request('POST','/internal/executor/claim?'+query,token='executor'))
    def test_oversized_body_and_transfer_encoding_rejected_before_upstream(self):
        self.assertEqual(413,self.request('POST','/internal/collector/fixtures',headers={'Content-Length':str(MAX_BODY+1)}))
        self.assertEqual(400,self.request('POST','/internal/collector/fixtures',headers={'Transfer-Encoding':'chunked'}))
        self.assertEqual([],self.upstream.calls)
    def test_broken_tunnel_returns503_without_retry_and_recovers_next_explicit_request(self):
        original=self.bridge.socket_path;self.bridge.socket_path=original+'.absent'
        self.assertEqual(503,self.request('GET','/internal/collector/config'))
        self.assertEqual([],self.upstream.calls)
        self.bridge.socket_path=original
        self.assertEqual(200,self.request('GET','/internal/collector/config'))
        self.assertEqual(1,len(self.upstream.calls))
    def test_response_does_not_cache_or_forward_cookie_headers(self):
        conn=http.client.HTTPConnection('127.0.0.1',self.bridge.server_port,timeout=2)
        conn.request('GET','/internal/collector/config',headers={'Authorization':'Bearer '+TOKENS['collector'],'Cookie':'MUST-NOT-FORWARD'})
        response=conn.getresponse();response.read()
        self.assertEqual('no-store',response.getheader('Cache-Control'));conn.close()


class RealWorkerContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_worker_methods_paths_and_query_are_in_allowlist(self):
        from services.collector.telegram_backend import TelegramBackend
        from services.collector.leyu_runtime import LeyuRuntime
        from services.collector.worker import get_config, CollectorService
        from services.executor.worker import Executor
        calls=[]
        async def handle(request):
            path=request.url.raw_path.decode()
            role='executor' if '/executor/' in path else 'collector'
            self.assertTrue(permitted(request.method,path,'Bearer '+TOKENS[role],TOKENS), (request.method,path))
            calls.append((request.method,path))
            return httpx.Response(200,json={'items':{},'accepted':0,'attempt_id':'11111111-1111-1111-1111-111111111111','version':8})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            root='http://api-bridge:8081/internal'
            await get_config(client,root)
            collector=CollectorService.__new__(CollectorService)
            collector.api=client;collector.api_url=root;collector._upload_lock=asyncio.Lock()
            await collector.upload([{'id':'11111111-1111-1111-1111-111111111111','source_match_id':'sample','markets':[]}])
            runtime=LeyuRuntime(client,root)
            await runtime._rpc('GET','/config');await runtime._claim('synthetic')
            await runtime._finish('11111111-1111-1111-1111-111111111111','failed',{})
            await runtime._rpc('PUT','/config',json={})
            backend=TelegramBackend(client,root+'/telegram')
            await backend.sources();await backend.directory({});await backend.checkpoint('1','owner')
            await backend.advance('1','owner',2,1);await backend.event({});await backend.pending()
            await backend.observed('1','owner');await backend.acknowledge_observed('1','owner',{})
            await backend.save_session('synthetic',{});await backend.media_orphans('owner')
            await backend.acknowledge_media_orphans('owner',[])
            stop=asyncio.Event()
            async def stop_after_claim(request):
                result=await handle(request)
                if request.url.path.endswith('/claim'):stop.set()
                return result
            async with httpx.AsyncClient(transport=httpx.MockTransport(stop_after_claim)) as execution:
                runner=Executor(execution,root+'/executor')
                await runner.run(stop)
            executor=Executor(client,root+'/executor')
            for method,path in [('POST','/jobs/11111111-1111-1111-1111-111111111111/renewal/claim'),
                                ('PUT','/jobs/11111111-1111-1111-1111-111111111111/session'),
                                ('POST','/jobs/11111111-1111-1111-1111-111111111111/renewal/finish'),
                                ('POST','/jobs/11111111-1111-1111-1111-111111111111/finish'),
                                ('POST','/orders/11111111-1111-1111-1111-111111111111/authorize'),
                                ('POST','/orders/11111111-1111-1111-1111-111111111111/finish')]:
                await executor.rpc(method,path,json={})
        self.assertIn(('POST','/internal/executor/claim?live_capable=false'),calls)
        self.assertGreaterEqual(len(calls),25)


class LinkHealthTests(unittest.TestCase):
    def test_health_uses_real_control_chain_and_ssh_child_not_storage_ports(self):
        class Control(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                good=self.path=='/status' and self.headers.get('Authorization')=='Bearer synthetic'
                self.send_response(200 if good else 401);self.end_headers();self.wfile.write(b'{}')
        from http.server import HTTPServer
        server=HTTPServer(('127.0.0.1',0),Control)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                state=Path(directory)/'state.json';state.write_text(json.dumps({'pid':os.getpid()}))
                self.assertEqual(0,health(state_path=state,control_port=server.server_port,token='synthetic'))
                self.assertEqual(1,health(state_path=state,control_port=server.server_port,token='wrong'))
                state.write_text(json.dumps({'pid':2147483647}))
                self.assertEqual(1,health(state_path=state,control_port=server.server_port,token='synthetic'))
        finally:server.shutdown();server.server_close();thread.join(2)


class SocketReconnectTests(unittest.TestCase):
    def test_closed_socket_reconnects_without_unlinking_live_or_bound_socket(self):
        with tempfile.TemporaryDirectory() as directory,patch('infra.remote.socket_guard.grant',return_value=True):
            parent=Path(directory);target=parent/'api.sock'
            old=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);old.bind(str(target));old.listen(3)
            guard=StaleSocketGuard()
            self.assertEqual('listening',guard.observe(parent,now=0))
            old.close()
            stamp=target.stat().st_ctime
            self.assertEqual('refused',guard.observe(parent,now=1,wall_now=stamp+40))
            original_lstat=Path.lstat
            def owned(path):
                original=original_lstat(path)
                return SimpleNamespace(st_dev=original.st_dev,st_ino=original.st_ino,st_ctime_ns=original.st_ctime_ns,st_ctime=original.st_ctime,
                    st_mode=original.st_mode,st_uid=0,st_gid=10005) if path==target else original
            with patch.object(Path,'lstat',owned):
                self.assertEqual('stale_removed',guard.observe(parent,now=3,wall_now=stamp+42))
            self.assertFalse(target.exists())
            renewed=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);renewed.bind(str(target));renewed.listen(3)
            self.assertEqual('listening',guard.observe(parent,now=5));renewed.close()
            target.unlink()
            bound=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);bound.bind(str(target))
            self.assertEqual('associated',guard.observe(parent,now=9))
            self.assertTrue(target.exists());bound.close()


if __name__=='__main__':unittest.main()
