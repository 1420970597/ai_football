"""有界赛果核验租约；只接受已关联推荐及期间证据完整的原生终场观测。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, Field, model_validator
from basketball_scores import validate_no_overtime_evidence, validate_provisional_evidence

from .. import database
from ..result_check_queue import claim_due_results
from ..result_ledger import ingest_result
from ..review_state import authorize
from .results import ResultInput

router = APIRouter(prefix='/internal/collector/results')
LEASE_SECONDS = 90
DETAIL_PATH = '/yewu11/v1/m/matchDetail/getMatchDetailPB'
RATE_LIMIT_REASONS = frozenset({'shared_upstream_cooldown', 'http_rate_limited', 'upstream_rate_limited'})


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
                "WHERE f.sport IN ('football','basketball') AND f.source_id LIKE 'leyu:%' AND f.kickoff_at<=now() "
                "ON CONFLICT DO NOTHING")
            rows = await claim_due_results(conn, body.limit)
            output = []
            for row in rows:
                output.append({'fixture_id':row['fixture_id'],'source_match_id':row['source_id'].removeprefix('leyu:'),
                               'kickoff_at':row['kickoff_at'],'home_team':row['home_team'],'away_team':row['away_team'],
                               'sport': row['sport'], 'lease_id':str(row['lease_id'])})
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

    def permits_period_conflict(self):
        if self.status != 'provisional' or self.evidence.get('sport') != '2':
            return False
        validate_provisional_evidence(self.home, self.away, self.half_home, self.half_away, self.evidence)
        return True

    @model_validator(mode='after')
    def validate_observation(self):
        if self.status not in {'confirmed', 'provisional'} or self.half_home is None or self.half_away is None:
            raise ValueError('原生详情必须提供确认或待核验全场及上半场比分')
        proof = self.evidence
        if (proof.get('source') != 'leyu_native_detail' or proof.get('path') != DETAIL_PATH
                or proof.get('sport') not in {'1', '2'} or proof.get('period') != '999'
                or proof.get('state') not in {'3','4'} or proof.get('score_period') != 'S1'
                or proof.get('source_time_quality') != 'unknown'):
            raise ValueError('原生终场证据不完整')
        if proof.get('sport') == '2':
            validator = validate_no_overtime_evidence if self.status == 'confirmed' else validate_provisional_evidence
            validator(self.home, self.away, self.half_home, self.half_away, proof)
        elif self.status != 'confirmed' or any(key in proof for key in ('score_scope', 'overtime', 'quarter_1', 'quarter_2', 'quarter_3', 'quarter_4')):
            raise ValueError('篮球期间证据不能标为足球')
        if not self.requested_at <= self.observed_at <= self.requested_at+timedelta(seconds=15):
            raise ValueError('赛果观测必须来自有界的当前请求')
        if (proof.get('full_time') != f'{self.home}:{self.away}'
                or proof.get('first_half') != f'{self.half_home}:{self.half_away}'
                or self.status == 'confirmed' and proof.get('second_half') != f'{self.home-self.half_home}:{self.away-self.half_away}'
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
            fixture = await conn.fetchrow('SELECT source_id,sport FROM v2_fixtures WHERE id=$1',body.fixture_id)
            if (fixture['source_id'] != 'leyu:'+body.evidence.get('mid','')
                    or {'football': '1', 'basketball': '2'}.get(fixture['sport']) != body.evidence.get('sport')
                    or body.provider_reference != 'leyu_native_detail:'+body.evidence.get('mid','')):
                raise HTTPException(422,'赛果来源与已领取比赛不一致')
            result = await ingest_result(conn,body)
            if datetime.now(timezone.utc) >= check['lease_until']:
                raise HTTPException(409,'赛果核验租约在提交前已失效')
            await conn.execute(
                "UPDATE v2_result_checks SET lease_id=NULL,lease_until=NULL,failures=0,last_reason=$2,"
                "last_checked_at=now(),next_check_at=now()+CASE WHEN $3::text='confirmed' THEN interval '5 minutes' "
                "ELSE interval '30 seconds' END WHERE fixture_id=$1",body.fixture_id,
                body.evidence.get('conflict_reason', 'confirmed'),body.status)
    return result


class FinishInput(CheckIdentity):
    reason: str = Field(pattern='^[a-z][a-z0-9_]{0,79}$')
    retry_after_seconds: int | None = Field(default=None, ge=1, le=900, strict=True)

    @model_validator(mode='after')
    def validate_retry(self):
        if self.retry_after_seconds is not None and self.reason not in RATE_LIMIT_REASONS:
            raise ValueError('仅限流或共享冷却可以提交重试时间')
        return self


@router.post('/finish')
async def finish_check(body: FinishInput, request: Request):
    authorize(request, request.app.state.settings.collector_token)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            await require_lease(conn,body)
            waiting = body.reason == 'result_not_final'
            await conn.execute(
                "UPDATE v2_result_checks SET lease_id=NULL,lease_until=NULL,last_reason=$2,last_checked_at=now(),"
                "next_check_at=now()+make_interval(secs=>CASE WHEN $3 THEN 30 WHEN $4 THEN COALESCE($5::int,60) "
                "ELSE LEAST(1800,30*power(2,LEAST(failures,5)))::int END),"
                "failures=CASE WHEN $3 THEN 0 WHEN $2='shared_upstream_cooldown' THEN failures ELSE failures+1 END "
                "WHERE fixture_id=$1", body.fixture_id, body.reason, waiting,
                body.reason in RATE_LIMIT_REASONS, body.retry_after_seconds)
    return {'ok':True}
