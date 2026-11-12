"""只核验专家关联赛事；原生终场详情与源修订时间分开记录。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import time

import httpx
from basketball_scores import no_overtime_scores, provisional_conflict_scores

from .leyu import DEFAULT_RESULT_DETAIL_PATH, LeyuClient
from .leyu_protocol import LeyuError, RateLimited, score, timestamp


def normalize_result(detail: dict, target: dict, *, requested_at: datetime, observed_at: datetime) -> dict:
    if not detail:
        raise LeyuError('result_detail_unavailable')
    mid = str(detail.get('mid') or '')
    if mid != target['source_match_id']:
        raise LeyuError('result_fixture_mismatch')
    sport = target.get('sport', 'football')
    expected_sport = {'football': '1', 'basketball': '2'}.get(sport)
    if (expected_sport is None or str(detail.get('csid')) != expected_sport or not timestamp(detail.get('mgt'))
            or timestamp(detail.get('mgt')) != timestamp(target['kickoff_at'])
            or detail.get('mhn') != target['home_team'] or detail.get('man') != target['away_team']):
        raise LeyuError('result_identity_mismatch')
    if str(detail.get('mmp')) != '999' or str(detail.get('ms')) not in {'3','4'}:
        raise LeyuError('result_not_final')
    if sport == 'basketball':
        return _basketball_result(detail, target, requested_at=requested_at, observed_at=observed_at)
    if str(detail.get('mft')) != '0':
        raise LeyuError('result_period_unsupported')
    scores = detail.get('msc')
    if not isinstance(scores,list) or any(sum(str(s).startswith(p+'|') for s in scores) != 1 for p in ('S1','S2','S3')):
        raise LeyuError('result_score_evidence_incomplete')
    full, half, second = (score(scores,p) for p in ('S1','S2','S3'))
    if any(v is None or v>200 for pair in (full,half,second) for v in pair):
        raise LeyuError('result_score_evidence_incomplete')
    if full != (half[0]+second[0],half[1]+second[1]):
        raise LeyuError('result_period_scores_conflict')
    # S7 is explicitly OVERTIME in the verified native client. This adapter
    # accepts only regular-time facts, never an aggregate extra-time result.
    if any(str(s).startswith('S7|') for s in scores):
        raise LeyuError('result_period_unsupported')
    digest = hashlib.sha256(json.dumps(detail,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    return {**{k:target[k] for k in ('fixture_id','lease_id')},'provider_reference':'leyu_native_detail:'+mid,
            'status':'confirmed','home':full[0],'away':full[1],'half_home':half[0],'half_away':half[1],
            'source_updated_at':None,'requested_at':requested_at.isoformat(),'observed_at':observed_at.isoformat(),
            'evidence':{'source':'leyu_native_detail','path':DEFAULT_RESULT_DETAIL_PATH,'mid':mid,'sport':'1',
                        'period':'999','state':str(detail['ms']),'score_period':'S1','source_time_quality':'unknown',
                        'full_time':f'{full[0]}:{full[1]}','first_half':f'{half[0]}:{half[1]}',
                        'second_half':f'{second[0]}:{second[1]}','decoded_response_sha256':digest}}


def _basketball_result(detail, target, *, requested_at, observed_at):
    # mft 0 and 7 were both observed in verified ordinary basketball finals.
    # It is not an overtime flag; establish no overtime from complete periods.
    if str(detail.get('mft')) not in {'0', '7'}:
        raise LeyuError('result_period_unsupported')
    try:
        periods = no_overtime_scores(detail.get('msc'))
    except ValueError as exc:
        if str(exc) not in {'basketball_overtime_rule_unverified', 'basketball_overtime_or_period_scores_conflict'}:
            raise LeyuError(str(exc)) from None
        try:
            periods, conflict_reason = provisional_conflict_scores(detail.get('msc'))
        except ValueError as conflict:
            raise LeyuError(str(conflict)) from None
    else:
        conflict_reason = None
    full, half, second = (periods[p] for p in ('S1', 'S2', 'S3'))
    digest = hashlib.sha256(json.dumps(detail, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return {**{k: target[k] for k in ('fixture_id', 'lease_id')},
            'provider_reference': 'leyu_native_detail:' + str(detail['mid']),
            'status': 'provisional' if conflict_reason else 'confirmed', 'home': full[0], 'away': full[1], 'half_home': half[0], 'half_away': half[1],
            'source_updated_at': None, 'requested_at': requested_at.isoformat(), 'observed_at': observed_at.isoformat(),
            'evidence': {'source': 'leyu_native_detail', 'path': DEFAULT_RESULT_DETAIL_PATH, 'mid': str(detail['mid']),
                         'sport': '2', 'period': '999', 'state': str(detail['ms']), 'mft': str(detail['mft']),
                         'score_period': 'S1', 'source_time_quality': 'unknown',
                         'score_scope': 'full_time_unverified' if conflict_reason else 'full_time_no_overtime',
                         'overtime': ('observed:' + ':'.join(map(str, periods['S7'])) if 'S7' in periods else 'unverified') if conflict_reason else 'none_verified',
                         **({'conflict_reason': conflict_reason} if conflict_reason else {}),
                         'full_time': f'{full[0]}:{full[1]}',
                         'first_half': f'{half[0]}:{half[1]}', 'second_half': f'{second[0]}:{second[1]}',
                         **{'quarter_' + str(i): ':'.join(map(str, periods[p]))
                            for i, p in enumerate(('S19', 'S20', 'S21', 'S22'), 1)},
                         'decoded_response_sha256': digest}}


async def fetch_result(client: LeyuClient, target: dict) -> dict:
    mid = client.validate_match_id(target['source_match_id'])
    requested = datetime.now(timezone.utc)
    async with asyncio.timeout(12):
        async with httpx.AsyncClient(timeout=httpx.Timeout(8,connect=3),transport=client.transport,
                                     trust_env=False,follow_redirects=False) as http:
            detail = await client._request(http,'GET',DEFAULT_RESULT_DETAIL_PATH,
                                           params={'mid':mid,'rdm':str(int(time.time()*1000))})
    if not isinstance(detail,dict):
        raise LeyuError('invalid_result_detail')
    return normalize_result(detail,target,requested_at=requested,observed_at=datetime.now(timezone.utc))


class ResultCollector:
    def __init__(self, api, api_url, runtime, status):
        self.api,self.url,self.runtime,self.status = api,api_url.rstrip('/')+'/collector/results',runtime,status

    async def check(self, target, client, version):
        try:
            self.runtime.request_budget.require_available()
            body = await fetch_result(client,target)
            if version != self.runtime.version:
                raise LeyuError('result_configuration_changed')
            response = await self.api.post(self.url+'/observations',json=body)
            response.raise_for_status()
            return {'confirmed':int(body['status'] == 'confirmed'),'settled':response.json().get('settled',0),
                    **({'provisional':1,'reason':body['evidence']['conflict_reason']} if body['status']=='provisional' else {})}
        except asyncio.CancelledError:
            # A cancelled request never submits facts; its persisted lease expires.
            raise
        except Exception as exc:
            retry = {}
            if isinstance(exc,RateLimited):
                # Re-arming a cooldown this lane only observed would shut the
                # shared budget for good: this lane runs every thirty seconds,
                # so it would always see the remaining time before it expires,
                # defer a fresh sixty seconds, and never let any lane through.
                # Measured before this fix: one provider 429 left the budget
                # blocked indefinitely, and 124 of 383 market refreshes over a
                # day were rejected with upstream_cooldown. Only a limit the
                # provider itself returned is new evidence of pressure; a gate
                # this process already honoured is reported, not extended.
                if exc.category == 'shared_upstream_cooldown':
                    retry['retry_after_seconds'] = max(1, exc.retry_after_seconds or 1)
                else:
                    delay = max(60,exc.retry_after_seconds or 0)
                    self.runtime.request_budget.defer_requests(delay)
                    retry['retry_after_seconds'] = delay
            reason = exc.category if isinstance(exc,LeyuError) else 'result_transport_failure' if isinstance(exc,httpx.HTTPError) else 'result_check_failed'
            response = await self.api.post(self.url+'/finish',json={**{k:target[k] for k in ('fixture_id','lease_id')},'reason':reason,**retry})
            response.raise_for_status()
            return {'confirmed':0,'settled':0,'reason':reason}

    async def run_once(self):
        self.runtime.request_budget.require_available()
        await self.runtime.load()
        client,version = LeyuClient.from_config(self.runtime.config),self.runtime.version
        response = await self.api.post(self.url+'/claim',json={'limit':4})
        response.raise_for_status()
        targets = response.json()['items']
        outcomes = []
        # Two source reads at most, four matches per cycle; this lane neither
        # renews credentials nor scans every fixture or shares the Telegram lock.
        for start in range(0,len(targets),2):
            batch = await asyncio.gather(*(self.check(t,client,version) for t in targets[start:start+2]),return_exceptions=True)
            outcomes.extend({'confirmed':0,'settled':0,'reason':'result_delivery_unconfirmed'}
                            if isinstance(item,Exception) else item for item in batch)
        self.status.update(state='partial' if any(o.get('reason') for o in outcomes) else 'healthy',
                           checked=len(outcomes),confirmed=sum(o['confirmed'] for o in outcomes),
                           settled=sum(o['settled'] for o in outcomes),
                           last_reasons=sorted({o['reason'] for o in outcomes if o.get('reason')}),
                           checked_at=datetime.now(timezone.utc).isoformat())

    async def run(self, stop):
        while not stop.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status.update(state='unavailable',last_reason=exc.category if isinstance(exc,LeyuError) else 'result_service_unavailable',
                                   checked_at=datetime.now(timezone.utc).isoformat())
            try:
                await asyncio.wait_for(stop.wait(),timeout=30)
            except TimeoutError:
                pass
