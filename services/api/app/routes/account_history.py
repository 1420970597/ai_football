"""Bounded read-only account-history requests, owned by the signed-in user."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .. import account_jobs as jobs, database
from ..account_history import summarize
from ..auth import require_user
from .accounts import service_auth

router = APIRouter(prefix='/api/v1/accounts/history')
executor_router = APIRouter(prefix='/internal/executor')


class HistoryItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    order_no: str = Field(min_length=1, max_length=160)
    identifier_kind: Literal['order', 'detail']
    state: Literal['settled', 'unsettled']
    match_id: str | None = Field(default=None, min_length=1, max_length=160)
    stake: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=5)
    payout: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=5)
    price: Decimal | None = Field(default=None, gt=1, max_digits=12, decimal_places=5)
    net_profit: Decimal | None = Field(default=None, max_digits=16, decimal_places=5)
    fees: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=5)
    pnl_basis: Literal['unknown', 'provider_reported_net', 'gross_return_minus_stake'] = 'unknown'
    currency: str | None = Field(default=None, pattern='^[A-Z]{3}$')
    placed_at: AwareDatetime
    settled_at: AwareDatetime | None = None
    market_name: str | None = Field(default=None, max_length=200)
    selection_name: str | None = Field(default=None, max_length=200)


class HistoryFinish(BaseModel):
    model_config = ConfigDict(extra='forbid')
    attempt_id: uuid.UUID
    complete: bool
    observed_at: AwareDatetime
    items: list[HistoryItem] = Field(default_factory=list, max_length=2000)
    error_category: str = Field(default='', pattern='^[a-zA-Z0-9_]{0,80}$')


async def projection(conn, user_id):
    account = await conn.fetchrow("SELECT id,account_label,binding_revision,health_state FROM v2_leyu_accounts "
                                  "WHERE user_id=$1 AND health_state<>'disabled'", user_id)
    if not account:
        return {'status': 'unbound', 'matches': [], 'association_complete': False}
    row = await conn.fetchrow('SELECT h.*,j.state AS job_state,j.last_error FROM v2_account_history h '
                              'JOIN v2_account_jobs j ON j.id=h.job_id WHERE h.account_id=$1 '
                              'AND h.user_id=$2 AND h.binding_revision=$3',
                              account['id'], user_id, account['binding_revision'])
    base = {'account_id': str(account['id']), 'account_label': account['account_label'],
            'binding_revision': account['binding_revision'], 'timezone': 'UTC',
            'scope': 'orders_placed_in_last_7_days', 'match_scope': 'whole_fixture',
            'association_complete': False, 'matches': []}
    if not row:
        return {**base, 'status': 'not_loaded'}
    status = row['job_state']
    if status == 'succeeded':
        status = 'completed' if row['complete'] else 'partial'
    elif status not in {'queued', 'running'}:
        status = 'failed'
    return {**base, **summarize(row['items'], row['complete'] and status == 'completed'),
            'status': status, 'window_start': row['window_start'], 'window_end': row['window_end'],
            'observed_at': row['observed_at'], 'requested_at': row['requested_at'],
            'error_category': row['error_category'] or ('history_job_failed' if status == 'failed' else '')}


@router.get('')
async def history(request: Request):
    user = await require_user(request)
    async with database.current_pool().acquire() as conn:
        return await projection(conn, user['id'])


@router.post('/refresh')
async def refresh(request: Request):
    user = await require_user(request)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            account = await conn.fetchrow("SELECT * FROM v2_leyu_accounts WHERE user_id=$1 "
                                          "AND health_state<>'disabled' FOR UPDATE", user['id'])
            if not account:
                return {'status': 'unbound', 'matches': [], 'association_complete': False}
            previous = await conn.fetchrow('SELECT * FROM v2_account_history WHERE account_id=$1 '
                                           'AND binding_revision=$2', account['id'], account['binding_revision'])
            now = datetime.now(timezone.utc)
            if previous and now - previous['requested_at'] < timedelta(seconds=60):
                return await projection(conn, user['id'])
            if account['health_state'] != 'healthy' or not account['encrypted_session']:
                return {'status': 'verification_required', 'matches': [], 'association_complete': False}
            busy = await conn.fetchval("SELECT 1 FROM v2_account_jobs WHERE account_id=$1 "
                                       "AND state IN('queued','running')", account['id'])
            if busy or account['renewal_state'] == 'running':
                return {**(await projection(conn, user['id'])), 'status': 'account_busy', 'retry_after': 3}
            job = await jobs.enqueue(conn, account, 'history')
            await conn.execute('INSERT INTO v2_account_history(account_id,user_id,binding_revision,job_id,window_start,window_end) '
                               'VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(account_id) DO UPDATE SET '
                               'user_id=EXCLUDED.user_id,binding_revision=EXCLUDED.binding_revision,job_id=EXCLUDED.job_id,'
                               "window_start=EXCLUDED.window_start,window_end=EXCLUDED.window_end,requested_at=now(),"
                               "observed_at=NULL,complete=false,items='[]',error_category=''",
                               account['id'], user['id'], account['binding_revision'], job['id'], now-timedelta(days=7), now)
            return await projection(conn, user['id'])


async def work_request(conn, job):
    if job['kind'] != 'history':
        return None
    row = await conn.fetchrow('SELECT window_start,window_end FROM v2_account_history WHERE job_id=$1', job['id'])
    if not row:
        raise HTTPException(409, '历史任务缺少查询范围')
    return {'begin': row['window_start'].isoformat(), 'end': row['window_end'].isoformat()}


@executor_router.post('/jobs/{job_id}/history/finish')
async def finish(job_id: uuid.UUID, body: HistoryFinish, request: Request):
    service_auth(request)
    document = body.model_dump(mode='json')
    digest = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            prior = await conn.fetchrow('SELECT * FROM v2_account_jobs WHERE id=$1', job_id)
            if prior and prior['state'] == 'succeeded' and prior['attempt_id'] == body.attempt_id:
                if prior['details'].get('history_digest') != digest:
                    raise HTTPException(409, '历史回执冲突')
                return {'status': 'saved', 'idempotent': True}
            job, account = await jobs.active_job(conn, job_id, body.attempt_id)
            if job['kind'] != 'history':
                raise HTTPException(409, '不是历史任务')
            row = await conn.fetchrow('SELECT * FROM v2_account_history WHERE job_id=$1 AND account_id=$2 '
                                      'AND binding_revision=$3 FOR UPDATE', job_id, account['id'], account['binding_revision'])
            if not row:
                raise HTTPException(409, '历史查询绑定已改变')
            if (not -5 <= (datetime.now(timezone.utc)-body.observed_at).total_seconds() <= 120
                    or body.observed_at < row['window_end']
                    or body.complete and body.error_category):
                raise HTTPException(422, '历史回执时间或完整性无效')
            keys = [(item.identifier_kind, item.order_no) for item in body.items]
            if len(set(keys)) != len(keys) or any(not row['window_start'] <= i.placed_at <= row['window_end'] for i in body.items):
                raise HTTPException(422, '历史回执重复或超出七日查询范围')
            await conn.execute('UPDATE v2_account_history SET observed_at=$2,complete=$3,items=$4,error_category=$5 WHERE job_id=$1',
                               job_id, body.observed_at, body.complete, document['items'], body.error_category)
            await conn.execute("UPDATE v2_account_jobs SET state='succeeded',lease_until=NULL,updated_at=now(),"
                               "details=details||$2::jsonb WHERE id=$1", job_id, {'history_digest': digest})
    return {'status': 'saved'}
