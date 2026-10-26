"""本人账户、异步校验、订单许可与独立执行器受限接口。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import uuid
from urllib.parse import urlsplit

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .. import account_jobs as jobs, database, orders
from ..auth import _limit, audit, require_user
from ..membership import has_entitlement
from ..review_state import authorize
from ..security import encrypt_secret

router = APIRouter(prefix="/api/v1/accounts")
executor_router = APIRouter(prefix="/internal/executor")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LeyuBindingInput(Input):
    account_label: str = Field(min_length=1, max_length=80)
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=256)


class AutoBetInput(Input):
    enabled: bool
    confirmation_phrase: str = ""
    allowed_experts: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    stake_per_order: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    max_per_order: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    daily_limit: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    open_exposure_limit: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)


class AttemptInput(Input):
    attempt_id: uuid.UUID


class RenewalClaim(AttemptInput):
    expected_revision: int = Field(ge=1)
    identity_hash: str = Field(pattern="^[a-f0-9]{64}$")


class PersistInput(AttemptInput):
    expected_revision: int = Field(ge=1)
    renewal_attempt: uuid.UUID
    updates: dict[str, str] = Field(min_length=1, max_length=5)


class RenewalFinish(AttemptInput):
    renewal_attempt: uuid.UUID
    status: str = Field(pattern="^(success|failed|unknown|rejected)$")
    metadata: dict = Field(default_factory=dict)


class Receipt(Input):
    order_no: str = Field(min_length=1, max_length=160)
    state: str = Field(pattern="^(settled|unsettled)$")
    net_profit: Decimal | None = Field(default=None, max_digits=16, decimal_places=5)
    pnl_basis: str = Field(default="unknown", pattern="^(unknown|provider_reported_net|gross_return_minus_stake)$")
    fees: Decimal | None = None


class JobFinish(AttemptInput):
    status: str = Field(pattern="^(success|failed|unknown|rejected)$")
    category: str = Field(default="", max_length=80, pattern="^[A-Za-z0-9_]*$")
    balance: Decimal | None = Field(default=None, ge=0, max_digits=16, decimal_places=2)
    observed_at: AwareDatetime | None = None
    receipts: list[Receipt] = Field(default_factory=list, max_length=10)
    scanned_orders: list[uuid.UUID] = Field(default_factory=list,max_length=2)


class AuthorizeInput(AttemptInput):
    prepared_digest: str = Field(pattern="^[a-f0-9]{64}$")
    payload_json: str = Field(min_length=1, max_length=20000)


class OrderFinish(AttemptInput):
    state: str = Field(pattern="^(accepted|pending|rejected|unknown|cancelled)$")
    order_no: str | None = Field(default=None, max_length=160)
    category: str = Field(default="", max_length=80, pattern="^[A-Za-z0-9_]*$")
    provider_code: str = Field(default="", pattern="^[0-9]{0,12}$")


def service_auth(request):
    authorize(request, getattr(request.app.state.settings, "executor_token", ""))


@router.get("/leyu")
async def get_leyu_account(request: Request):
    user = await require_user(request)
    row = await database.current_pool().fetchrow("SELECT id,account_label,health_state,last_verified_at,updated_at,available_balance,balance_observed_at,config_revision,binding_revision,renewal_state "
                                                "FROM v2_leyu_accounts WHERE user_id=$1 AND health_state<>'disabled'", user["id"])
    policy = await database.current_pool().fetchrow("SELECT auto_enabled,allowed_experts,stake_per_order,max_per_order,daily_limit,open_exposure_limit,emergency_stopped,revision "
                                                   "FROM v2_user_bet_settings WHERE user_id=$1", user["id"])
    tasks = await database.current_pool().fetch("SELECT id,kind,state,last_error,updated_at FROM v2_account_jobs WHERE user_id=$1 ORDER BY created_at DESC LIMIT 5", user["id"])
    return {"account": dict(row) if row else None, "betting": dict(policy) if policy else None, "tasks": [dict(item) for item in tasks],
            "execution_capability": {"protocol_adapter": True, "live_enabled": orders.live_enabled(request.app.state.settings), "prematch": "unsupported"}}


@router.put("/leyu")
async def bind_leyu_account(body: LeyuBindingInput, request: Request):
    user = await require_user(request)
    await _limit(request,'account_bind',str(user['id']),maximum=6)
    username, label = body.username.strip(), body.account_label.strip()
    if not username or not label or any(char in username for char in "\r\n"):
        raise HTTPException(422, "账户名称无效")
    settings = request.app.state.settings
    encrypted = encrypt_secret(settings.encryption_key, json.dumps({"username": username, "password": body.password, "device_uuid": uuid.uuid4().hex}, separators=(",", ":")))
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"user-account:{user['id']}")
            await conn.fetchrow('SELECT id FROM v2_leyu_accounts WHERE user_id=$1 FOR UPDATE',user['id'])
            await orders.protect_inflight_binding(conn,user['id'])
            try:
                account = await conn.fetchrow("INSERT INTO v2_leyu_accounts(id,user_id,encrypted_credentials,account_label,provider_identity_hash) VALUES($1,$2,$3,$4,$5) "
                    "ON CONFLICT(user_id) DO UPDATE SET encrypted_credentials=EXCLUDED.encrypted_credentials,account_label=EXCLUDED.account_label,provider_identity_hash=EXCLUDED.provider_identity_hash,"
                    "encrypted_session=NULL,health_state='unverified',config_revision=v2_leyu_accounts.config_revision+1,binding_revision=v2_leyu_accounts.binding_revision+1,"
                    "last_verified_at=NULL,available_balance=NULL,balance_observed_at=NULL,renewal_state='ready',renewal_attempt=NULL,renewal_lease_until=NULL,renewal_retry_after=NULL,updated_at=now() RETURNING *",
                    uuid.uuid4(), user["id"], encrypted, label, jobs.identity_hash(settings.encryption_key, username))
            except asyncpg.UniqueViolationError:
                raise HTTPException(409, "该乐鱼身份已经绑定其它平台账户") from None
            await conn.execute("INSERT INTO v2_user_bet_settings(user_id) VALUES($1) ON CONFLICT DO NOTHING", user["id"])
            await conn.execute("UPDATE v2_user_bet_settings SET auto_enabled=FALSE,emergency_stopped=TRUE,consent_account_id=NULL,consent_binding_revision=NULL,revision=revision+1,updated_at=now() WHERE user_id=$1", user["id"])
            await orders.cancel_unsent(conn, user["id"], "account_rebound")
            await conn.execute("UPDATE v2_account_jobs SET state='failed',last_error='account_rebound',updated_at=now() WHERE account_id=$1 AND state IN('queued','running')", account["id"])
            task = await jobs.enqueue(conn, account, "verify")
            await audit(conn, user["id"], "user", "leyu_account_bound", "leyu_account", str(account["id"]), details={"binding_revision": account["binding_revision"]})
    return {"state": "unverified", "account_label": label, "task_id": str(task["id"]), "message": "本人凭据已加密保存，已排队进行无资金校验；原有自动投注许可已撤销。"}


@router.delete("/leyu")
async def unbind_leyu_account(request: Request):
    user = await require_user(request)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            await conn.fetchrow("SELECT id FROM v2_leyu_accounts WHERE user_id=$1 FOR UPDATE", user["id"])
            await orders.protect_inflight_binding(conn,user['id'])
            await conn.execute("UPDATE v2_leyu_accounts SET health_state='disabled',encrypted_credentials='',encrypted_session=NULL,provider_identity_hash=NULL,config_revision=config_revision+1,binding_revision=binding_revision+1,updated_at=now() WHERE user_id=$1", user["id"])
            await disable_policy(conn, user["id"], "account_unbound")
            await conn.execute("UPDATE v2_account_jobs SET state='failed',last_error='account_unbound',updated_at=now() WHERE user_id=$1 AND state IN('queued','running')", user["id"])
            await audit(conn, user["id"], "user", "leyu_account_unbound", "leyu_account", str(user["id"]))
    return {"unbound": True, "auto_betting": False}


async def disable_policy(conn, user_id, reason):
    await conn.execute("INSERT INTO v2_user_bet_settings(user_id,auto_enabled,emergency_stopped) VALUES($1,FALSE,TRUE) ON CONFLICT(user_id) DO UPDATE SET auto_enabled=FALSE,emergency_stopped=TRUE,revision=v2_user_bet_settings.revision+1,updated_at=now()", user_id)
    await orders.cancel_unsent(conn, user_id, reason)


@router.post("/leyu/verify", status_code=202)
@router.post("/leyu/refresh", status_code=202)
async def verify_account(request: Request):
    user = await require_user(request)
    await _limit(request,'account_verify',str(user['id']),maximum=10)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            account = await conn.fetchrow("SELECT * FROM v2_leyu_accounts WHERE user_id=$1 FOR UPDATE", user["id"])
            if not account or account["health_state"] == "disabled":
                raise HTTPException(404, "尚未绑定本人账户")
            if account["renewal_state"] in {"unknown", "rejected"}:
                raise HTTPException(409, "上次登录未知或被拒绝，核对后需重新绑定以授权")
            task = await jobs.enqueue(conn, account, "refresh" if account["health_state"] == "healthy" else "verify")
    return {"task_id": str(task["id"]), "state": task["state"]}


@router.put("/leyu/auto-betting")
async def configure_auto_betting(body: AutoBetInput, request: Request):
    user = await require_user(request)
    settings = request.app.state.settings
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            account = await conn.fetchrow("SELECT * FROM v2_leyu_accounts WHERE user_id=$1 FOR UPDATE", user["id"])
            if not body.enabled:
                await disable_policy(conn, user["id"], "user_disabled")
                return {"enabled": False, "emergency_stopped": True}
            if body.confirmation_phrase != "I_UNDERSTAND" or not body.allowed_experts:
                raise HTTPException(422, "需要明确本人许可并选择允许的专家")
            if min(body.stake_per_order, body.max_per_order, body.daily_limit, body.open_exposure_limit) <= 0 or body.stake_per_order > body.max_per_order or body.max_per_order > min(body.daily_limit, body.open_exposure_limit):
                raise HTTPException(422, "投注限额关系无效")
            if not account or account["health_state"] != "healthy" or not account["encrypted_session"]:
                raise HTTPException(409, "本人乐鱼账户尚未通过无资金校验")
            if not orders.live_enabled(settings) or not await orders.global_enabled(conn, settings):
                raise HTTPException(403, "实际交易尚未被开放，开发能力不构成实盘授权")
            if not await has_entitlement(user["id"], "automatic_betting", connection=conn):
                raise HTTPException(403, "当前会员没有自动投注权益")
            valid = await conn.fetchval("SELECT count(*) FROM v2_experts WHERE id=ANY($1::uuid[]) AND enabled", list(set(body.allowed_experts)))
            if valid != len(set(body.allowed_experts)):
                raise HTTPException(422, "允许专家包含无效条目")
            await conn.execute("INSERT INTO v2_user_bet_settings(user_id,auto_enabled,allowed_experts,stake_per_order,max_per_order,daily_limit,open_exposure_limit,emergency_stopped,consent_account_id,consent_binding_revision) "
                "VALUES($1,TRUE,$2,$3,$4,$5,$6,FALSE,$7,$8) ON CONFLICT(user_id) DO UPDATE SET auto_enabled=TRUE,allowed_experts=EXCLUDED.allowed_experts,"
                "stake_per_order=EXCLUDED.stake_per_order,max_per_order=EXCLUDED.max_per_order,daily_limit=EXCLUDED.daily_limit,open_exposure_limit=EXCLUDED.open_exposure_limit,"
                "emergency_stopped=FALSE,consent_account_id=EXCLUDED.consent_account_id,consent_binding_revision=EXCLUDED.consent_binding_revision,revision=v2_user_bet_settings.revision+1,updated_at=now()",
                user["id"], list(set(body.allowed_experts)), body.stake_per_order, body.max_per_order, body.daily_limit, body.open_exposure_limit, account["id"], account["binding_revision"])
            await orders.cancel_unsent(conn, user["id"], "policy_changed")
            await audit(conn, user["id"], "user", "auto_betting_authorized", "leyu_account", str(account["id"]), details={"binding_revision": account["binding_revision"]})
    return {"enabled": True, "emergency_stopped": False, "state": "armed"}


@router.post("/leyu/emergency-stop")
async def emergency_stop(request: Request):
    user = await require_user(request)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            await conn.fetchrow("SELECT id FROM v2_leyu_accounts WHERE user_id=$1 FOR UPDATE", user["id"])
            await disable_policy(conn, user["id"], "emergency_stop")
            await audit(conn, user["id"], "user", "emergency_stop", "betting_settings", str(user["id"]))
    return {"enabled": False, "emergency_stopped": True}


@router.get("/orders")
async def list_orders(request: Request, limit: int = 30, before: uuid.UUID | None = None):
    user = await require_user(request)
    if not 1 <= limit <= 100:
        raise HTTPException(422, "订单页大小无效")
    rows = await database.current_pool().fetch("SELECT o.id,o.state,o.stake,o.price,o.created_at,o.updated_at,o.provider_order_no,o.reserved_stake,o.settled_net,o.settled_at,"
        "s.fixture_id,s.market_name,s.line,s.selection,e.display_nickname FROM v2_bet_orders o JOIN v2_expert_signals s ON s.id=o.signal_id "
        "JOIN v2_experts e ON e.id=s.expert_id WHERE o.user_id=$1 AND ($2::uuid IS NULL OR (o.created_at,o.id)<"
        "(SELECT created_at,id FROM v2_bet_orders WHERE id=$2 AND user_id=$1)) ORDER BY o.created_at DESC,o.id DESC LIMIT $3", user["id"], before, limit)
    return {"items": [dict(row) for row in rows], "next_cursor": str(rows[-1]["id"]) if len(rows) == limit else None}


@executor_router.post("/claim")
async def claim_work(request: Request, live_capable: bool = False):
    service_auth(request)
    settings = request.app.state.settings
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            await jobs.expire_leases(conn)
            account = await conn.fetchrow("SELECT a.* FROM v2_leyu_accounts a WHERE a.health_state<>'disabled' AND EXISTS(SELECT 1 FROM v2_account_jobs j WHERE j.account_id=a.id AND j.state='queued' AND j.binding_revision=a.binding_revision) ORDER BY a.updated_at FOR UPDATE OF a SKIP LOCKED LIMIT 1")
            if account:
                job = await conn.fetchrow("SELECT * FROM v2_account_jobs WHERE account_id=$1 AND state='queued' AND binding_revision=$2 ORDER BY created_at LIMIT 1 FOR UPDATE", account["id"], account["binding_revision"])
                config = await jobs.private_config(conn, account, settings)
                attempt = uuid.uuid4()
                await conn.execute("UPDATE v2_account_jobs SET state='running',attempt_id=$2,config_revision=$3,lease_until=now()+interval '300 seconds',updated_at=now() WHERE id=$1", job["id"], attempt, account["config_revision"])
                reconcile = await conn.fetch("SELECT id,provider_order_no,created_at FROM v2_bet_orders WHERE user_id=$1 AND provider_identity_hash=$2 AND state IN('accepted','unknown') AND provider_order_no IS NOT NULL ORDER BY updated_at LIMIT 2", account["user_id"], account["provider_identity_hash"])
                await conn.execute('UPDATE v2_account_jobs SET details=$2 WHERE id=$1',job['id'],{'orders':{str(row['id']):row['provider_order_no'] for row in reconcile}})
                return {"kind": "account", "job_id": str(job["id"]), "attempt_id": str(attempt), "scope": jobs.scope(account), "config": config, "orders": [dict(row) for row in reconcile]}
            if live_capable and orders.live_enabled(settings):
                account = await conn.fetchrow("SELECT a.* FROM v2_leyu_accounts a WHERE EXISTS(SELECT 1 FROM v2_bet_orders o WHERE o.account_id=a.id AND o.state='pending' AND o.attempt_id IS NULL) ORDER BY a.updated_at FOR UPDATE OF a SKIP LOCKED LIMIT 1")
                if account:
                    order = await conn.fetchrow("SELECT * FROM v2_bet_orders WHERE account_id=$1 AND state='pending' AND attempt_id IS NULL ORDER BY created_at LIMIT 1 FOR UPDATE", account["id"])
                    try:
                        await orders.recheck_order(conn, order, settings)
                    except HTTPException as exc:
                        await conn.execute("UPDATE v2_bet_orders SET state='cancelled',reserved_stake=0,result_payload=$2,updated_at=now() WHERE id=$1", order["id"], {"reason": "eligibility_changed", "status": exc.status_code})
                        return {"kind": "idle"}
                    config = await jobs.private_config(conn, account, settings)
                    attempt = uuid.uuid4()
                    await conn.execute("UPDATE v2_bet_orders SET attempt_id=$2,lease_until=now()+interval '45 seconds',updated_at=now() WHERE id=$1", order["id"], attempt)
                    return {"kind": "order", "order_id": str(order["id"]), "attempt_id": str(attempt), "scope": jobs.scope(account), "config": config, "selection": order["quote_snapshot"], "stake": str(order["stake"])}
    return {"kind": "idle"}


@executor_router.post("/dispatch")
async def dispatch(request: Request):
    service_auth(request)
    settings = request.app.state.settings
    count = 0
    async with database.current_pool().acquire() as conn:
        if orders.live_enabled(settings):
            pairs = await conn.fetch("SELECT b.user_id,s.id AS signal_id FROM v2_user_bet_settings b JOIN v2_expert_signals s ON s.expert_id=ANY(b.allowed_experts) "
                "WHERE b.auto_enabled AND NOT b.emergency_stopped AND s.created_at>now()-interval '5 minutes' AND s.outcome='pending' "
                "AND NOT EXISTS(SELECT 1 FROM v2_bet_orders o WHERE o.user_id=b.user_id AND o.signal_id=s.id AND o.account_id IS NOT NULL) ORDER BY s.created_at LIMIT 30")
            for pair in pairs:
                try:
                    async with conn.transaction():
                        await orders.reserve_signal(conn, pair["user_id"], pair["signal_id"], settings)
                        count += 1
                except HTTPException:
                    continue
        async with conn.transaction():
            rows = await conn.fetch("SELECT a.* FROM v2_leyu_accounts a WHERE a.health_state='healthy' AND (a.last_verified_at<now()-interval '60 seconds' OR EXISTS(SELECT 1 FROM v2_bet_orders o WHERE o.user_id=a.user_id AND o.state IN('accepted','unknown') AND o.provider_order_no IS NOT NULL AND o.updated_at<now()-interval '30 seconds')) "
                "AND NOT EXISTS(SELECT 1 FROM v2_account_jobs j WHERE j.account_id=a.id AND j.state IN('queued','running')) ORDER BY a.updated_at FOR UPDATE OF a SKIP LOCKED LIMIT 20")
            for account in rows:
                await jobs.enqueue(conn, account, "refresh")
    return {"reserved": count}


@executor_router.post("/jobs/{job_id}/renewal/claim")
async def claim_account_renewal(job_id: uuid.UUID, body: RenewalClaim, request: Request):
    service_auth(request)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            job, account = await jobs.active_job(conn, job_id, body.attempt_id)
            if account["config_revision"] != body.expected_revision:
                raise HTTPException(409, "个人账户配置版本冲突")
            credentials = jobs.decode_document(request.app.state.settings.encryption_key, account["encrypted_credentials"])
            expected = hashlib.sha256(("2001:" + credentials["username"].casefold()).encode()).hexdigest()
            if expected != body.identity_hash or account["renewal_state"] != "ready" or (account["renewal_retry_after"] and account["renewal_retry_after"] > datetime.now(timezone.utc)):
                raise HTTPException(409, "本人续期被拒绝、未知、进行中或冷却，不能重复登录")
            attempt = uuid.uuid4()
            await conn.execute("UPDATE v2_leyu_accounts SET renewal_state='running',renewal_attempt=$2,renewal_lease_until=now()+interval '180 seconds',updated_at=now() WHERE id=$1", account["id"], attempt)
            await audit(conn, None, "executor", "account_renewal_claimed", "leyu_account", str(account["id"]))
    return {"attempt_id": str(attempt)}


@executor_router.put("/jobs/{job_id}/session")
async def persist_account_session(job_id: uuid.UUID, body: PersistInput, request: Request):
    service_auth(request)
    if set(body.updates) - jobs.SESSION_KEYS:
        raise HTTPException(422, "个人续期不能写其它配置")
    for key, value in body.updates.items():
        if not value or len(value)>8192 or any(char in value for char in "\r\n"):
            raise HTTPException(422, "个人会话值无效")
        if key in {"leyu_api_base", "leyu_origin", "leyu_app_host"}:
            parts = urlsplit(value)
            if parts.scheme!='https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ('','/'):
                raise HTTPException(422, "个人会话来源必须HTTPS")
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            job, account = await jobs.active_job(conn, job_id, body.attempt_id)
            if (account["config_revision"] != body.expected_revision or account["renewal_state"] != 'running' or account["renewal_attempt"] != body.renewal_attempt
                    or account["renewal_lease_until"] <= datetime.now(timezone.utc)):
                raise HTTPException(409, "个人会话版本或续期租约失效")
            document = jobs.decode_document(request.app.state.settings.encryption_key, account["encrypted_session"]) if account["encrypted_session"] else {}
            document.update(body.updates)
            encrypted = encrypt_secret(request.app.state.settings.encryption_key, json.dumps(document,separators=(',',':')))
            revision = await conn.fetchval("UPDATE v2_leyu_accounts SET encrypted_session=$2,config_revision=config_revision+1,updated_at=now() WHERE id=$1 RETURNING config_revision", account["id"], encrypted)
            await conn.execute("UPDATE v2_account_jobs SET config_revision=$2 WHERE id=$1", job_id, revision)
            await audit(conn,None,'executor','account_session_persisted','leyu_account',str(account['id']),details={'keys':sorted(body.updates),'revision':revision})
    return {"revision":revision}


@executor_router.post("/jobs/{job_id}/renewal/finish")
async def finish_account_renewal(job_id: uuid.UUID, body: RenewalFinish, request: Request):
    service_auth(request)
    if set(body.metadata)-{'category','code','login_submitted'} or not re.fullmatch('[A-Za-z0-9_]{0,80}',str(body.metadata.get('category',''))) or not re.fullmatch('[0-9]{0,12}',str(body.metadata.get('code',''))):
        raise HTTPException(422,'续期审计字段无效')
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            job,account=await jobs.active_job(conn,job_id,body.attempt_id)
            if (job['details'].get('renewal_attempt')==str(body.renewal_attempt)
                    and job['details'].get('renewal_status')==body.status):
                return {'state':account['renewal_state'],'idempotent':True}
            if account['renewal_attempt']!=body.renewal_attempt or account['renewal_state']!='running':
                raise HTTPException(409,'个人续期任务已失效')
            expired=account['renewal_lease_until']<=datetime.now(timezone.utc)
            state='unknown' if expired else body.status if body.status in {'unknown','rejected'} else 'ready'
            await conn.execute("UPDATE v2_leyu_accounts SET renewal_state=$2,renewal_lease_until=NULL,renewal_retry_after=now()+interval '60 seconds',updated_at=now() WHERE id=$1",account['id'],state)
            await conn.execute("UPDATE v2_account_jobs SET details=details||$2::jsonb WHERE id=$1",job_id,{'renewal_attempt':str(body.renewal_attempt),'renewal_status':'unknown' if expired else body.status})
            await audit(conn,None,'executor','account_renewal_finished','leyu_account',str(account['id']),details={'state':state,**body.metadata})
    return {'state':state}


@executor_router.post('/jobs/{job_id}/finish')
async def finish_account_job(job_id: uuid.UUID,body:JobFinish,request:Request):
    service_auth(request)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            prior=await conn.fetchrow('SELECT * FROM v2_account_jobs WHERE id=$1',job_id)
            expected='succeeded' if body.status=='success' else body.status
            if prior and prior['attempt_id']==body.attempt_id and prior['state']==expected:
                return {'status':body.status,'idempotent':True}
            job,account=await jobs.active_job(conn,job_id,body.attempt_id)
            if body.status=='success':
                document=jobs.decode_document(request.app.state.settings.encryption_key,account['encrypted_session']) if account['encrypted_session'] else {}
                now=datetime.now(timezone.utc)
                if body.balance is None or not body.observed_at or not -5<=(now-body.observed_at).total_seconds()<=60 or not document.get('leyu_request_id'):
                    raise HTTPException(422,'无资金校验缺少有效余额、观测时间或本人会话')
                planned=job['details'].get('orders',{})
                if any(str(identity) not in planned for identity in body.scanned_orders) or any(receipt.order_no not in planned.values() for receipt in body.receipts):
                    raise HTTPException(422,'查单回执超出此本人任务的订单范围')
                await conn.execute("UPDATE v2_leyu_accounts SET health_state='healthy',last_verified_at=now(),available_balance=$2,balance_observed_at=$3,updated_at=now() WHERE id=$1",account['id'],body.balance,body.observed_at)
                await jobs.reconcile_receipts(conn,account,[receipt.model_dump(mode='json') for receipt in body.receipts])
                await conn.execute('UPDATE v2_bet_orders SET updated_at=now() WHERE id=ANY($1::uuid[]) AND user_id=$2',body.scanned_orders,account['user_id'])
            elif body.status in {'unknown','rejected'}:
                await conn.execute("UPDATE v2_leyu_accounts SET health_state=$2,updated_at=now() WHERE id=$1",account['id'],'rejected' if body.status=='rejected' else 'renewal_required')
            await conn.execute("UPDATE v2_account_jobs SET state=$2,last_error=$3,lease_until=NULL,details=$4,updated_at=now() WHERE id=$1",job_id,'succeeded' if body.status=='success' else body.status,body.category,{'receipts':len(body.receipts)})
            await audit(conn,None,'executor','account_job_finished','leyu_account',str(account['id']),details={'kind':job['kind'],'status':body.status,'category':body.category})
    return {'status':body.status}


async def locked_order(conn,order_id):
    row=await conn.fetchrow('SELECT user_id FROM v2_bet_orders WHERE id=$1',order_id)
    if not row:
        raise HTTPException(404,'订单不存在')
    await conn.fetchrow('SELECT id FROM v2_leyu_accounts WHERE user_id=$1 FOR UPDATE',row['user_id'])
    return await conn.fetchrow('SELECT * FROM v2_bet_orders WHERE id=$1 FOR UPDATE',order_id)


@executor_router.post('/orders/{order_id}/authorize')
async def authorize_order(order_id:uuid.UUID,body:AuthorizeInput,request:Request):
    service_auth(request)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            order=await locked_order(conn,order_id)
            if order['state']!='pending' or order['attempt_id']!=body.attempt_id or not order['lease_until'] or order['lease_until']<=datetime.now(timezone.utc):
                raise HTTPException(409,'订单准备租约失效')
            account=await orders.recheck_order(conn,order,request.app.state.settings)
            prefix=f"{account['user_id']}:{account['id']}:{account['config_revision']}:"
            if hashlib.sha256((prefix+body.payload_json).encode()).hexdigest()!=body.prepared_digest:
                raise HTTPException(422,'订单准备摘要不匹配')
            try:
                payload=json.loads(body.payload_json)
                series=payload['seriesOrders']
                details=series[0]['orderDetailList']
                detail=details[0]
                snapshot=order['quote_snapshot']
                credentials=jobs.decode_document(request.app.state.settings.encryption_key,account['encrypted_credentials'])
                valid=(len(series)==1 and len(details)==1 and type(payload['acceptOdds']) is int and payload['acceptOdds']==2 and str(payload['preBet'])=='0'
                    and payload['currencyCode']=='CNY' and str(payload['deviceType'])=='3' and payload['openMiltSingle']==0
                    and str(series[0]['seriesType'])=='1' and series[0]['seriesSum']==1 and series[0]['fullBet']==0
                    and detail['marketTypeFinally']=='EU' and detail['matchType']==2 and str(detail['sportId'])=='1'
                    and orders.prepared_line(detail['marketValue'],snapshot['kind'])==Decimal(snapshot['line'])
                    and detail['scoreBenchmark']==snapshot['score_benchmark']
                    and payload['deviceImei']==credentials['device_uuid'] and Decimal(detail['betAmount'])==order['stake']
                    and Decimal(detail['oddFinally'])==order['price'] and Decimal(detail['odds'])==order['price']*100000
                    and all(str(detail[target])==str(snapshot[source]) for target,source in [('matchId','match_id'),('marketId','market_instance_id'),('playId','play_id'),('playOptionsId','selection_id'),('playOptions','selection_code')]))
            except (ValueError,KeyError,IndexError,TypeError,InvalidOperation):
                valid=False
            if not valid:
                raise HTTPException(422,'订单准备内容超出已预留推荐许可')
            await conn.execute("UPDATE v2_bet_orders SET state='submitting',prepared_digest=$2,submitted_at=now(),lease_until=now()+interval '30 seconds',updated_at=now() WHERE id=$1",order_id,body.prepared_digest)
            await audit(conn,None,'executor','order_send_authorized','bet_order',str(order_id))
    return {'scope':jobs.scope(account),'execution_id':str(order_id),'prepared_digest':body.prepared_digest}


@executor_router.post('/orders/{order_id}/finish')
async def finish_order(order_id:uuid.UUID,body:OrderFinish,request:Request):
    service_auth(request)
    async with database.current_pool().acquire() as conn:
        async with conn.transaction():
            order=await locked_order(conn,order_id)
            if order['attempt_id']!=body.attempt_id:
                raise HTTPException(409,'订单执行尝试不匹配')
            if order['state'] in {'accepted','rejected','settled','cancelled'}:
                if body.state==order['state'] and (not body.order_no or body.order_no==order['provider_order_no']):
                    return {'state':order['state'],'idempotent':True}
                raise HTTPException(409,'资金订单终态不可覆盖')
            if order['state']=='pending':
                if body.state!='cancelled':
                    raise HTTPException(409,'未获得发送许可的订单只能取消')
                target='cancelled'
            else:
                if body.state not in {'accepted','pending','rejected','unknown'}:
                    raise HTTPException(409,'已发送订单不能按未发送取消')
                if body.state in {'accepted','pending'} and not body.order_no:
                    raise HTTPException(422,'接单或确认中回执缺少订单号')
                target='accepted' if body.state in {'accepted','pending'} else body.state
            await conn.execute("UPDATE v2_bet_orders SET state=$2,provider_order_no=COALESCE($3,provider_order_no),reserved_stake=CASE WHEN $2 IN('rejected','cancelled') THEN 0 ELSE reserved_stake END,"
                "lease_until=NULL,result_payload=$4,updated_at=now() WHERE id=$1",order_id,target,body.order_no,{'category':body.category,'provider_code':body.provider_code,'provider_state':body.state})
            await audit(conn,None,'executor','order_finished','bet_order',str(order_id),details={'state':target,'category':body.category})
    return {'state':target,'idempotent':False}
