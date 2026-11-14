"""One configuration revision, bounded read-only probes, redacted release diagnosis."""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import ssl
from urllib.parse import urlsplit
import urllib.error
import urllib.request

from .deployment import project_path, validate_manifest

MAX_RESPONSE = 1024 * 1024
CHECKS = ('api', 'collector', 'database', 'objects', 'hermes')
STATES = {'ready', 'missing', 'invalid', 'unreachable', 'expired', 'unverified'}


def summarize(manifest: dict, config: dict, probes: dict, *, finished_revision=None) -> dict:
    """All input content is private. Output includes only fixed keys/reasons/revision."""
    version = manifest.get('configuration_version')
    findings = []
    for reason in validate_manifest(manifest):
        findings.append({'component': 'deployment', 'state': 'invalid', 'reason': reason})
    for name in CHECKS:
        value = probes.get(name, {})
        state = value.get('state', 'unverified')
        if state not in STATES:
            state = 'invalid'
        # Accept only predeclared status; arbitrary exception strings never leave.
        findings.append({'component': name, 'state': state,
                         'reason': {'ready':'probe_verified', 'missing':'configuration_missing',
                                    'invalid':'configuration_invalid', 'unreachable':'probe_unreachable',
                                    'expired':'authorization_expired', 'unverified':'not_verified'}[state]})
    expected = config.get('version')
    applied = probes.get('collector', {}).get('configuration_version')
    if type(expected) is not int or expected != version or type(finished_revision) is not int or finished_revision != expected:
        findings.append({'component':'configuration', 'state':'invalid', 'reason':'configuration_revision_changed_or_unknown'})
    if type(applied) is not int or applied != expected:
        findings.append({'component':'collector', 'state':'invalid', 'reason':'configuration_not_consumed'})
    return {'checked_at':datetime.now(timezone.utc).isoformat(), 'configuration_version':version,
            'source_commit':manifest.get('source_commit') if not validate_manifest(manifest) else None,
            'ready':all(item['state']=='ready' for item in findings), 'findings':findings,
            'requests_read_only':True, 'login_performed':False, 'bet_performed':False,
            'deletion_performed':False, 'production_restore_verified':False}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_):
        raise ValueError('redirect_rejected')


class ProbeClient:
    def __init__(self, *, api_base, collector_base, collector_token, bridge_verified=False):
        self.api_base = api_base.rstrip('/')
        self.collector_base = collector_base.rstrip('/')
        self.token = collector_token
        for base in (self.api_base, self.collector_base):
            parsed = urlsplit(base)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('probe_target_invalid')
            secure = parsed.scheme == 'https'
            protected = bridge_verified and parsed.scheme == 'http' and (parsed.hostname,parsed.port) in {
                ('api-bridge',8081), ('api',8080), ('remote-link',18092), ('collector',8092)}
            if not secure and not protected:
                raise ValueError('probe_transport_unverified')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect,
                                                  urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def request(self, suffix, *, control=False):
        request = urllib.request.Request((self.collector_base if control else self.api_base) + suffix,
            data=b'{}' if control else None, method='POST' if control else 'GET',
            headers={'Authorization':'Bearer '+self.token, 'Content-Type':'application/json'})
        try:
            with self.opener.open(request, timeout=4) as response:
                content = response.read(MAX_RESPONSE+1)
                if len(content)>MAX_RESPONSE:
                    return {'state':'invalid'}
                value=json.loads(content)
                if not isinstance(value,dict):
                    return {'state':'invalid'}
                return value
        except urllib.error.HTTPError as exc:
            return {'state':'expired' if exc.code in {401,403} else 'unreachable'}
        except (ValueError, UnicodeError):
            return {'state':'invalid'}
        except OSError:
            return {'state':'unreachable'}


def classify_collector(value):
    if value.get('state') in STATES:
        return {'state':value['state']}
    leyu=value.get('source_health',{}).get('leyu',{})
    if leyu.get('last_error') in {'session_expired','authorized_session_missing','app_token_expired'}:
        state='expired'
    elif leyu.get('config_validation_error'):
        state='invalid'
    elif (leyu.get('state') == 'healthy' and leyu.get('freshness') == 'fresh'
          and not leyu.get('last_error') and leyu.get('failed_batches', 0) == 0
          and value.get('connected') is True and value.get('authorized') is True
          and value.get('source_health', {}).get('telegram', {}).get('degraded') is False):
        state='ready'
    elif leyu.get('state') in {'healthy','partial','degraded','starting'}:
        # Reachability and a partial snapshot cannot prove business readiness.
        state='unverified'
    else:
        state='unreachable'
    return {'state':state,'configuration_version':leyu.get('config_version')}


async def collect(manifest, environment, *, api_base, collector_base, bridge_verified=False):
    from infra.config.storage_probe import database_probe, object_probe
    token=environment.get('COLLECTOR_INTERNAL_TOKEN','')
    if not token:
        return summarize(manifest,{}, {'api':{'state':'missing'}},finished_revision=None)
    client=ProbeClient(api_base=api_base,collector_base=collector_base,collector_token=token,
                       bridge_verified=bridge_verified)
    async with asyncio.timeout(20):
        before=await asyncio.to_thread(client.request,'/collector/renewal/config')
        version=before.get('version')
        control,db,objects=await asyncio.gather(asyncio.to_thread(client.request,'/status',control=True),
            database_probe(environment),asyncio.to_thread(object_probe,environment))
        after=await asyncio.to_thread(client.request,'/collector/renewal/config')
    probes={'api':{'state':'ready' if type(version) is int else before.get('state','invalid')},
            'collector':classify_collector(control),
            'database':{'state':'ready' if db.get('state')=='read_only_verified' and db.get('least_privilege_verified') is True else
                        'invalid' if db.get('state')=='read_only_verified' else 'missing' if db.get('state')=='unconfigured' else 'unreachable'},
            'objects':{'state':'ready' if objects.get('state')=='read_only_verified' else
                       'missing' if objects.get('state')=='unconfigured' else 'unreachable'},
            # This worker-only preflight does not prove the local model can complete a task.
            'hermes':{'state':'unverified'}}
    return summarize(manifest,{'version':version},probes,finished_revision=after.get('version'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('/root/ai_football'))
    parser.add_argument('--manifest',required=True)
    parser.add_argument('--observations')
    parser.add_argument('--probe',action='store_true')
    parser.add_argument('--api-base',default='http://api:8080/internal')
    parser.add_argument('--collector-base',default='http://remote-link:18092')
    parser.add_argument('--ssh-bridge-verified',action='store_true')
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        manifest=json.loads(project_path(args.root,args.manifest).read_text())
        if args.probe:
            import os
            result=asyncio.run(collect(manifest,dict(os.environ),api_base=args.api_base,
                collector_base=args.collector_base,bridge_verified=args.ssh_bridge_verified))
        else:
            evidence=json.loads(project_path(args.root,args.observations).read_text())
            result=summarize(manifest,evidence.get('config',{}),evidence.get('probes',{}),
                             finished_revision=evidence.get('finished_revision'))
        output=project_path(args.root,args.output)
        output.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        with output.open('x') as stream:
            import os
            os.chmod(output,0o600)
            json.dump(result,stream,sort_keys=True)
        print(json.dumps({'ready':result['ready'],'configuration_version':result['configuration_version'],
                          'findings':result['findings']},sort_keys=True))
        return 0 if result['ready'] else 1
    except Exception:
        print(json.dumps({'ready':False,'findings':[{'component':'input','state':'invalid','reason':'preflight_input_invalid'}]}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
