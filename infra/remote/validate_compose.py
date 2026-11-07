"""Validate the rendered local overlay and remote topology without starting it."""
import json
from pathlib import Path
import re
import sys


def validate(local, remote):
    local_services=local['services'];services=remote['services']
    assert set(services)=={'socket-guard','api-bridge','collector','executor'}, 'remote_service_boundary'
    assert local_services['api']['environment']['TELEGRAM_CONTROL_URL']=='http://remote-link:18092', 'control_target'
    for role in ('collector','executor'):
        assert local_services[role]['profiles']==['legacy-local-workers'], 'local_worker_not_profiled'
    link=local_services['remote-link']
    assert link['tmpfs']==['/tmp:rw,noexec,nosuid,nodev,size=16m'], 'link_tmpfs_options_split'
    assert link['healthcheck']['test']==['CMD','python','/deployment/ssh_link.py','--health'], 'inherited_wrong_health'
    assert link['networks'].keys()=={'app-private','egress'}, 'local_link_networks'
    assert services['executor']['environment']['EXECUTOR_LIVE_ORDERS_ENABLED']=='false', 'remote_live_orders'
    for role,size in (('collector','64m'),('executor','32m')):
        assert services[role]['tmpfs']==['/tmp:rw,noexec,nosuid,nodev,size='+size], 'worker_tmpfs_options_split'
    assert services['collector']['environment']['PLATFORM_API']=='http://api-bridge:8081/internal', 'collector_bridge'
    assert services['executor']['environment']['PLATFORM_EXECUTOR_API']=='http://api-bridge:8081/internal/executor', 'executor_bridge'
    assert services['collector']['environment']['S3_ENDPOINT_URL'].startswith('https://'), 'objects_tls_required'
    assert services['collector']['environment']['S3_USE_TLS']=='true', 'objects_tls_required'
    assert services['api-bridge']['user']=='10006:10005', 'bridge_privileges'
    assert services['socket-guard']['network_mode']=='host', 'kernel_socket_visibility'
    assert not services['socket-guard'].get('environment'), 'socket_guard_secret'
    for name,service in services.items():
        assert re.fullmatch(r'[a-z0-9./_-]+@sha256:[a-f0-9]{64}',service['image']), 'mutable_remote_image'
        assert service.get('read_only') is True and service.get('cap_drop')==['ALL'], 'privilege_boundary'
        assert service.get('cpus') and service.get('mem_limit') and service.get('pids_limit'), 'resource_budget'
        for mount in service.get('volumes',[]):
            assert mount['type']=='bind' and not mount['bind']['create_host_path'], 'implicit_host_write'
            assert str(Path('/root/ai_football')) in mount['source'], 'outside_project_mount'
            assert mount.get('read_only') is True or name=='socket-guard' and mount['target']=='/run/remote', 'writable_secret_mount'
        assert name=='collector' or not service.get('ports'), 'public_remote_port'
    ports=services['collector']['ports']
    assert len(ports)==1 and ports[0]['host_ip']=='127.0.0.1' and ports[0]['target']==8092 and str(ports[0]['published'])=='18092', 'control_public_binding'
    assert set(services['api-bridge']['networks'])=={'workers'} and remote['networks']['workers']['internal'], 'bridge_egress'
    return {'validated':True,'service_count':len(services),'started':False,'secrets_used':False}


if __name__=='__main__':
    print(json.dumps(validate(*(json.loads(Path(name).read_text()) for name in sys.argv[1:]))))
