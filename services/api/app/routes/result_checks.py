"""有界赛果核验租约；只接受已关联专家推荐的原生足球终场观测。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, Field, model_validator

from .. import database
from ..result_ledger import ingest_result
from ..review_state import authorize
from .results import ResultInput

router = APIRouter(prefix='/internal/collector/results')
LEASE_SECONDS = 90
DETAIL_PATH = '/yewu11/v1/m/matchDetail/getMatchDetailPB'


class ClaimInput(BaseModel):
    limit: int = Field(default=4, ge=1, le=4)


@router.post('/claim')
async def claim_results(body: ClaimInput, request: Request):
    authorize(request, request.app.state.settings.collector_token)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO v2_result_checks(fixture_id) SELECT DISTINCT s.fixture_id FROM "
                "(SELECT fixture_id FROM v2_expert_signals WHERE created_at>now()-interval '30 days' "
                "ORDER BY created_at DESC LIMIT 5000) s JOIN v2_fixtures f ON f.id=s.fixture_id "
                "WHERE f.sport='football' AND f.source_id LIKE 'leyu:%' AND f.kickoff_at<=now() "
                "ON CONFLICT DO NOTHING")
            rows = await conn.fetch(
                "SELECT c.fixture_id,f.source_id,f.kickoff_at,f.home_team,f.away_team FROM v2_result_checks c JOIN v2_fixtures f ON f.id=c.fixture_id "
                "WHERE c.next_check_at<=now() AND (c.lease_until IS NULL OR c.lease_until<now()) "
                "AND f.kickoff_at>now()-interval '30 days' "
                "AND (f.phase='finished' OR f.kickoff_at<=now()-interval '100 minutes') "
                "AND NOT EXISTS(SELECT 1 FROM v2_fixture_results r WHERE r.fixture_id=f.id "
                "AND r.version=(SELECT max(x.version) FROM v2_fixture_results x WHERE x.fixture_id=f.id) "
                "AND r.status='confirmed' AND r.received_at<now()-interval '7 days') "
                "ORDER BY c.next_check_at,c.fixture_id LIMIT $1 FOR UPDATE OF c SKIP LOCKED", body.limit)
            output = []
            for row in rows:
                lease = uuid.uuid4()
                await conn.execute("UPDATE v2_result_checks SET lease_id=$2,lease_until=now()+interval '90 seconds' WHERE fixture_id=$1",row['fixture_id'],lease)
                output.append({'fixture_id':row['fixture_id'],'source_match_id':row['source_id'].removeprefix('leyu:'),
                               'kickoff_at':row['kickoff_at'],'home_team':row['home_team'],'away_team':row['away_team'],'lease_id':str(lease)})
    return {'items':output,'lease_seconds':LEASE_SECONDS}


class CheckIdentity(BaseModel):
    fixture_id: str = Field(min_length=1,max_length=160)
    lease_id: uuid.UUID


async def require_lease(conn, body):
    row = await conn.fetchrow(
        "SELECT * FROM v2_result_checks WHERE fixture_id=$1 AND lease_id=$2 AND lease_until>clock_timestamp() FOR UPDATE",
        body.fixture_id,body.lease_id)
    if not row:
        raise HTTPException(409,'赛果核验租约已失效')
    return row


class ObservationInput(ResultInput, CheckIdentity):
    source_updated_at: None = None
    observed_at: AwareDatetime
    requested_at: AwareDatetime

    @model_validator(mode='after')
    def validate_observation(self):
        if self.status != 'confirmed' or self.half_home is None or self.half_away is None:
            raise ValueError('原生详情必须提供确认全场及上半场比分')
        proof = self.evidence
        if (proof.get('source') != 'leyu_native_detail' or proof.get('path') != DETAIL_PATH
                or proof.get('sport') != '1' or proof.get('period') != '999'
                or proof.get('state') not in {'3','4'} or proof.get('score_period') != 'S1'
                or proof.get('source_time_quality') != 'unknown'):
            raise ValueError('原生足球终场证据不完整')
        if not self.requested_at <= self.observed_at <= self.requested_at+timedelta(seconds=15):
            raise ValueError('赛果观测必须来自有界的当前请求')
        if (proof.get('full_time') != f'{self.home}:{self.away}'
                or proof.get('first_half') != f'{self.half_home}:{self.half_away}'
                or proof.get('second_half') != f'{self.home-self.half_home}:{self.away-self.half_away}'
                or not re.fullmatch('[0-9a-f]{64}', proof.get('decoded_response_sha256',''))):
            raise ValueError('比分期间与响应摘要证据不一致')
        return self


@router.post('/observations')
async def record_observation(body: ObservationInput, request: Request):
    authorize(request, request.app.state.settings.collector_token)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            check = await require_lease(conn,body)
            if body.requested_at < check['lease_until']-timedelta(seconds=LEASE_SECONDS):
                raise HTTPException(409,'赛果观测早于本次核验租约')
            source = await conn.fetchval('SELECT source_id FROM v2_fixtures WHERE id=$1',body.fixture_id)
            if source != 'leyu:'+body.evidence.get('mid','') or body.provider_reference != 'leyu_native_detail:'+body.evidence.get('mid',''):
                raise HTTPException(422,'赛果来源与已领取比赛不一致')
            result = await ingest_result(conn,body)
            if datetime.now(timezone.utc) >= check['lease_until']:
                raise HTTPException(409,'赛果核验租约在提交前已失效')
            await conn.execute(
                "UPDATE v2_result_checks SET lease_id=NULL,lease_until=NULL,failures=0,last_reason='confirmed',"
                "last_checked_at=now(),next_check_at=now()+interval '5 minutes' WHERE fixture_id=$1",body.fixture_id)
    return result


class FinishInput(CheckIdentity):
    reason: str = Field(pattern='^[a-z][a-z0-9_]{0,79}$')


@router.post('/finish')
async def finish_check(body: FinishInput, request: Request):
    authorize(request, request.app.state.settings.collector_token)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            await require_lease(conn,body)
            waiting = body.reason == 'result_not_final'
            await conn.execute(
                "UPDATE v2_result_checks SET lease_id=NULL,lease_until=NULL,last_reason=$2,last_checked_at=now(),"
                "next_check_at=now()+make_interval(secs=>CASE WHEN $3 THEN 30 ELSE LEAST(1800,30*power(2,LEAST(failures,5)))::int END),"
                "failures=CASE WHEN $3 THEN 0 ELSE failures+1 END WHERE fixture_id=$1",body.fixture_id,body.reason,waiting)
    return {'ok':True}
