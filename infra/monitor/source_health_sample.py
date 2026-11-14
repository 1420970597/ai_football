"""Read live source health in the API container; emit counts, never identities."""
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json

import httpx
from app.config import load_settings


def summarize(value, *, at):
    health = (value.get('source_health') or {}).get('telegram') or {}
    states = health.get('source_states') or {}
    configured = value.get('source_runtime') or {}
    errors = health.get('source_errors') or {}
    reasons = []
    if value.get('connected') is not True: reasons.append('disconnected')
    if value.get('authorized') is not True: reasons.append('unauthorized')
    if health.get('state') != 'connected': reasons.append('source_health_unavailable')
    if health.get('degraded') is not False or errors: reasons.append('source_degraded')
    try:
        last = datetime.fromisoformat(health['last_success_at'].replace('Z', '+00:00')).timestamp()
        if not 0 <= at-last <= 180: reasons.append('source_health_stale')
    except (KeyError, TypeError, ValueError):
        reasons.append('source_health_stale')
    if not configured or len(states) != len(configured): reasons.append('source_coverage_incomplete')
    if any(state not in {'caught_up', 'catching_up'} for state in states.values()):
        reasons.append('source_work_deferred')
    live = value.get('live_event_delivery') or {}
    return {'at': at, 'ok': not reasons, 'reasons': reasons,
            'configured_sources': len(configured), 'reported_sources': len(states),
            'source_states': dict(Counter(states.values())),
            'last_success_at': health.get('last_success_at'),
            'live_received': live.get('received'), 'live_staged': live.get('staged'),
            'live_deferred': live.get('deferred')}


async def sample():
    settings = load_settings()
    async with httpx.AsyncClient(trust_env=False, timeout=8,
        headers={'Authorization': 'Bearer '+settings.collector_token}) as client:
        response = await client.post('http://collector:8092/status', json={})
        response.raise_for_status()
    value = summarize(response.json(), at=datetime.now(timezone.utc).timestamp())
    # A collector heartbeat cannot prove the parsing tool process is ready.
    # Check its real DB-aware readiness, without any review or betting write.
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            ready = await client.get('http://local-mcp-api:8080/readyz')
            value['tool_api_ready'] = (ready.status_code == 200
                and ready.json().get('status') == 'ready' and ready.json().get('role') == 'mcp')
    except (httpx.HTTPError, ValueError, AttributeError):
        value['tool_api_ready'] = False
    if not value['tool_api_ready']:
        value['reasons'].append('tool_api_not_ready')
        value['ok'] = False
    return value


if __name__ == '__main__':
    print(json.dumps(asyncio.run(sample())))
